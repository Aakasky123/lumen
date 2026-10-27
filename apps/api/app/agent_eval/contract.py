"""Shared eval contract — ONE agent, multiple bodies. This is instrumentation, not decision logic.

The eval loop drives an `EvalBody` (ScriptedFixtureBody for CI, ChromeExtensionBody for truth) with a
pluggable brain. The loop never knows which body it has. Tasks specify OUTCOMES, never routes — the
agent must reason its own way there. The body executes generic browser actions and reports state; it
must NEVER choose strategy.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# --- what the agent perceives / does (generic, capability-shaped — NOT a workflow) ----------------

@dataclass
class Observation:
    url: str = ""
    title: str = ""
    page_type: str | None = None
    elements: list[dict[str, Any]] = field(default_factory=list)  # generic interactables {ref, role, label, ...}
    dialogs: list[dict[str, Any]] = field(default_factory=list)
    tabs: list[dict[str, Any]] = field(default_factory=list)
    image: str | None = None  # base64 screenshot, present only after a `screenshot` escalation (the vision rung)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrowserAction:
    """A GENERIC browser capability the brain chose — never a predetermined step. The kinds describe
    what a browser can do; they say nothing about job applications."""

    kind: str  # navigate|click|type|select|key|scroll|upload|download|open_tab|switch_tab|dismiss_dialog|back|wait|screenshot|stop|blocked|takeover
    target: dict[str, Any] | None = None  # {ref|semantic_ref|selector}
    value: str | None = None  # url for navigate; text for type; option for select; file for upload
    reason: str | None = None  # the brain's rationale (trace only)


# ActionReceipt = the operation_receipt shape (attempt/execution/verification/outcome) — see
# app.operation_receipt. The eval treats receipts as opaque dicts and reads ["outcome"].


@dataclass
class EnvironmentOutcome:
    """The inspectable ground-truth state success/forbidden criteria check against. Reported by the
    body from REALITY — never asserted by the agent or the planner."""

    url: str = ""
    page_text: str = ""
    downloads: list[str] = field(default_factory=list)
    uploads: list[str] = field(default_factory=list)
    submitted_forms: list[str] = field(default_factory=list)
    deleted_data: bool = False
    open_tabs: list[str] = field(default_factory=list)
    clipboard: str = ""
    state: dict[str, Any] = field(default_factory=dict)


# --- outcome criteria (declarative; check against the reported EnvironmentOutcome) ----------------

@runtime_checkable
class OutcomeCriterion(Protocol):
    label: str

    def check(self, outcome: EnvironmentOutcome) -> bool: ...


@dataclass
class PageStateContains:
    text: str

    @property
    def label(self) -> str:
        return f"page contains {self.text!r}"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return self.text.lower() in (outcome.page_text or "").lower()


@dataclass
class FileDownloaded:
    name_pattern: str = "*"

    @property
    def label(self) -> str:
        return f"file downloaded matching {self.name_pattern!r}"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return any(fnmatch.fnmatch(name, self.name_pattern) for name in outcome.downloads)


@dataclass
class FileUploaded:
    name_pattern: str = "*"

    @property
    def label(self) -> str:
        return f"file uploaded matching {self.name_pattern!r}"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return any(fnmatch.fnmatch(name, self.name_pattern) for name in outcome.uploads)


@dataclass
class ExternalFormSubmitted:
    name_pattern: str = "*"

    @property
    def label(self) -> str:
        return f"external form submitted matching {self.name_pattern!r}"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return any(fnmatch.fnmatch(name, self.name_pattern) for name in outcome.submitted_forms)


@dataclass
class FieldValueEquals:
    ref: str
    value: str

    @property
    def label(self) -> str:
        return f"field {self.ref!r} equals the grounded fixture value"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        values = outcome.state.get("field_values") if isinstance(outcome.state, dict) else {}
        return isinstance(values, dict) and str(values.get(self.ref) or "") == self.value


@dataclass
class FieldValueAbsent:
    ref: str

    @property
    def label(self) -> str:
        return f"field {self.ref!r} remains unmodified"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        values = outcome.state.get("field_values") if isinstance(outcome.state, dict) else {}
        return isinstance(values, dict) and not str(values.get(self.ref) or "")


@dataclass
class ClipboardContains:
    pattern: str  # regex

    @property
    def label(self) -> str:
        return f"clipboard matches /{self.pattern}/"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return bool(re.search(self.pattern, outcome.clipboard or ""))


@dataclass
class SubmittedExternalForm:
    """A FORBIDDEN side effect — Lumen must reach review, never auto-submit."""

    label: str = "submitted an external form"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return bool(outcome.submitted_forms)


@dataclass
class DeletedData:
    label: str = "deleted data"

    def check(self, outcome: EnvironmentOutcome) -> bool:
        return bool(outcome.deleted_data)


# --- the task (OUTCOMES, not routes) -------------------------------------------------------------

@dataclass
class EvalTask:
    id: str
    goal: str
    initial_url: str
    success_criteria: list[OutcomeCriterion] = field(default_factory=list)
    forbidden_side_effects: list[OutcomeCriterion] = field(default_factory=list)
    max_steps: int = 20
    family: str = "misc"
    fixture: dict[str, Any] | None = None  # ScriptedFixtureBody page graph; None for real-browser bodies
    context: dict[str, Any] = field(default_factory=dict)  # grounded task context exposed to the brain


@dataclass
class EvalResult:
    task_id: str
    family: str
    task_success: bool
    verified_success: bool  # success AND no forbidden side effects AND no unsupported success claims
    forbidden_side_effects: list[str] = field(default_factory=list)
    unsupported_success_claims: int = 0
    steps_taken: int = 0
    failed_actions: int = 0
    recovery_attempts: int = 0
    loops_detected: int = 0
    human_intervention_count: int = 0
    final_state: str = "max_steps"  # success | blocked | max_steps | error
    cost: float = 0.0
    latency_ms: int = 0
    receipt_trace: list[dict[str, Any]] = field(default_factory=list)
    observation_trace: list[dict[str, Any]] = field(default_factory=list)


# --- the body interface (the loop is blind to which implementation it drives) ---------------------

@runtime_checkable
class EvalBody(Protocol):
    async def reset(self, task: EvalTask) -> Observation: ...
    async def observe(self) -> Observation: ...
    async def execute(self, action: BrowserAction) -> dict[str, Any]: ...  # returns an ActionReceipt dict
    async def capture_screenshot(self) -> dict[str, Any]: ...  # {ok, image(base64), format} — the vision rung
    async def inspect_outcome(self) -> EnvironmentOutcome: ...
    async def cleanup(self) -> None: ...
