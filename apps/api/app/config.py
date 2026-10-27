import os
import sys
from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = (
    Path(getattr(sys, "_MEIPASS")).resolve()
    if getattr(sys, "frozen", False) and getattr(sys, "_MEIPASS", None)
    else Path(__file__).resolve().parents[3]
)
DATA_ROOT = Path(os.environ.get("LUMEN_DATA_ROOT") or PROJECT_ROOT / "storage").expanduser().resolve()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env", DATA_ROOT / ".env"),
        extra="ignore",
    )

    database_url: str = f"sqlite:///{DATA_ROOT / 'lumen.db'}"
    artifact_root: Path = Field(
        default=DATA_ROOT / "artifacts",
        validation_alias=AliasChoices("LUMEN_ARTIFACT_ROOT", "ARTIFACT_ROOT"),
    )
    openai_api_key: str | None = None
    use_mock_ai: bool = False
    # Run screenshots older than both windows are pruned (JSON records stay); 0 and 0 keeps everything.
    frame_keep_runs: int = Field(default=200, validation_alias="LUMEN_FRAME_KEEP_RUNS")
    frame_keep_days: int = Field(default=30, validation_alias="LUMEN_FRAME_KEEP_DAYS")
    desktop_control_enabled: bool = Field(default=False, validation_alias="LUMEN_DESKTOP_CONTROL")
    # While a run is working, hold a Windows power request so the display, sleep timer, and idle
    # screensavers don't kick in under it (see keep_awake.py). Tests turn it off.
    keep_awake_enabled: bool = Field(default=True, validation_alias="LUMEN_KEEP_AWAKE")
    # A/B override for the TOTP capability (the per-user agent setting `totp_capability` is the normal switch).
    totp_capability: bool = Field(default=False, validation_alias="LUMEN_TOTP_CAPABILITY")
    # Global input budget per brain step in estimated tokens (system + schema + history + packet + images;
    # token_budget.py). Over it, the packet is trimmed least-important first. The reduced cap applies to the
    # reduced-payload retry. 0 turns the budget off.
    prompt_input_token_cap: int = Field(default=40000, validation_alias="LUMEN_PROMPT_INPUT_TOKEN_CAP")
    prompt_input_token_cap_reduced: int = Field(default=20000, validation_alias="LUMEN_PROMPT_INPUT_TOKEN_CAP_REDUCED")
    # Compact the control inventory rows unchanged since the previous step (drop their dom_context and
    # ancestor_chain); full resync every few steps and after any page change, failure or reduced retry.
    brain_inventory_delta: bool = Field(default=False, validation_alias="LUMEN_BRAIN_INVENTORY_DELTA")
    # Opt-in brain message history (vision_agent.message_history_turns): the replies and outcomes of the
    # last few steps travel as earlier chat turns before each step's packet. Off by default (A/B switch);
    # the steps setting caps how many earlier steps are shown (the window is never larger).
    brain_message_history: bool = Field(default=False, validation_alias="LUMEN_BRAIN_MESSAGE_HISTORY")
    brain_message_history_steps: int = Field(default=8, validation_alias="LUMEN_BRAIN_MESSAGE_HISTORY_STEPS")
    # When the brain asks the user (ask_user), the in-browser loop publishes the question and waits
    # this long in place for the answer, keeping its memory; past it the run ends WAITING_FOR_USER
    # with the question published, and a later answer resumes the run from the body. The owner
    # answers from a phone while away, so the window is 30 minutes. 0 turns parking off. Tests turn it off.
    ask_user_park_seconds: float = Field(default=1800.0, validation_alias="LUMEN_ASK_USER_PARK_SECONDS")
    token_encryption_key: str | None = Field(default=None, validation_alias="LUMEN_TOKEN_ENCRYPTION_KEY")
    allow_generic_external_job_ingestion: bool = Field(default=False, validation_alias="LUMEN_ALLOW_GENERIC_EXTERNAL_JOB_INGESTION")
    # Default ON: the chat acknowledges immediately and the live run executes in a
    # post-response BackgroundTask (see main.post_chat_message). The run starts only after
    # the streaming request closes and releases its SQLite writes; a mid-stream start
    # deadlocks the write lock. Tests set LUMEN_CHAT_APPLICATION_ASYNC=false to run
    # the deferred application synchronously.
    chat_application_async: bool = Field(default=True, validation_alias="LUMEN_CHAT_APPLICATION_ASYNC")
    skill_author_model: str = Field(default="gpt-4o-mini", validation_alias="LUMEN_SKILL_AUTHOR_MODEL")
    skill_author_timeout_seconds: int = Field(default=30, validation_alias="LUMEN_SKILL_AUTHOR_TIMEOUT_SECONDS")
    # Vision provider used when the user's agent settings don't name one.
    vision_provider_id: int | None = Field(default=None, validation_alias="LUMEN_VISION_PROVIDER_ID")
    # Dev seam (external_brain.py): browser-step decisions go to files in this directory and wait
    # for an outside reasoner's reply instead of calling the vision provider. Unset in normal use.
    external_brain_dir: Path | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_BRAIN_DIR")
    external_brain_wait_seconds: float | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_BRAIN_WAIT_SECONDS")
    external_brain_purposes: str | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_BRAIN_PURPOSES")
    external_brain_per_run: bool = Field(default=True, validation_alias="LUMEN_EXTERNAL_BRAIN_PER_RUN")
    # Escalation mailbox (external_brain.py): step decisions made while the escalation brain holds a stuck part
    # of the task go here instead of the brain mailbox (same format, wait and per-run split). Unset: brain mailbox.
    external_escalation_dir: Path | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_ESCALATION_DIR")
    # Bench-only judge mailbox (external_judge.py): every non-brain model purpose is answered from files in
    # this directory (separate from the brain's). The wait floor raises, never lowers, a purpose's deadline.
    external_judge_dir: Path | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_JUDGE_DIR")
    external_judge_wait_seconds: float | None = Field(default=None, validation_alias="LUMEN_EXTERNAL_JUDGE_WAIT_SECONDS")
    # Bench judge seats take 10-40 s an answer: the mailbox waits at least this long whatever the purpose asks.
    external_judge_min_deadline_seconds: float = Field(default=90.0, validation_alias="LUMEN_EXTERNAL_JUDGE_MIN_DEADLINE_SECONDS")
    # Remote access for paired phones (app.remote_access). The backend serves a second, gated
    # listener on 127.0.0.1:<remote_port> that `tailscale serve` exposes to the tailnet; 0 turns it off.
    remote_port: int = Field(default=8002, validation_alias="LUMEN_REMOTE_PORT")
    # Base URL the phone uses; discovered from Tailscale when unset.
    remote_public_url: str | None = Field(default=None, validation_alias="LUMEN_REMOTE_PUBLIC_URL")
    # How long one fingerprint unlocks Full autonomy for tasks a paired phone starts.
    remote_autonomy_hours: float = Field(default=12, validation_alias="LUMEN_REMOTE_AUTONOMY_HOURS")
    # Firebase service-account JSON used to push alerts to paired phones (app.push). When unset,
    # %APPDATA%/Lumen/secrets/firebase-admin.json is used if it exists.
    firebase_credentials_path: Path | None = Field(default=None, validation_alias="LUMEN_FIREBASE_CREDENTIALS")
    # Which web_tools.SEARCH_BACKENDS function answers the brain's web_search (keyless by default).
    web_search_backend: str = Field(default="duckduckgo_html", validation_alias="LUMEN_WEB_SEARCH_BACKEND")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.artifact_root.mkdir(parents=True, exist_ok=True)
    return settings
