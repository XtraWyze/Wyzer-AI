"""Typed, JSON-serializable models shared across subsystem boundaries."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def utc_now() -> datetime:
    return datetime.now(UTC)


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    BLOCKED = "blocked"


class ConfirmationMode(StrEnum):
    NEVER = "never"
    ALWAYS = "always"
    CONDITIONAL = "conditional"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    NOT_VERIFIED = "not_verified"
    CONTRADICTED = "contradicted"
    UNAVAILABLE = "unavailable"


class ConsentStatus(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"
    DENIED = "denied"
    REVOKED = "revoked"


class EventKind(StrEnum):
    REQUEST_RECEIVED = "request_received"
    REFERENCE_RESOLVED = "reference_resolved"
    REFERENCE_UNRESOLVED = "reference_unresolved"
    PLAN_CREATED = "plan_created"
    PLAN_REJECTED = "plan_rejected"
    CONFIRMATION_REQUESTED = "confirmation_requested"
    CONFIRMATION_RECEIVED = "confirmation_received"
    CONFIRMATION_REJECTED = "confirmation_rejected"
    TOOL_STARTED = "tool_started"
    TOOL_COMPLETED = "tool_completed"
    TOOL_FAILED = "tool_failed"
    DESKTOP_ACTION = "desktop_action"
    PERCEPTION_SNAPSHOT = "perception_snapshot"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    TASK_INTERRUPTED = "task_interrupted"
    TASK_CANCELLED = "task_cancelled"
    RESPONSE_GENERATED = "response_generated"


class UserRequest(FrozenModel):
    request_id: UUID = Field(default_factory=uuid4)
    text: str = Field(min_length=1, max_length=20_000)
    received_at: datetime = Field(default_factory=utc_now)


class AssistantResponse(FrozenModel):
    text: str = Field(min_length=1)
    action_id: UUID
    interrupted: bool = False
    needs_clarification: bool = False


class ProviderDiagnostic(FrozenModel):
    provider: str
    available: bool
    endpoint: str | None = None
    model: str | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class ToolArguments(BaseModel):
    """Base class for each tool's concrete argument schema."""

    model_config = ConfigDict(extra="forbid")


class StructuredError(FrozenModel):
    code: str = Field(min_length=1)
    message: str = Field(min_length=1)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    tool: str = Field(min_length=1)
    action_id: UUID
    step_id: UUID
    started_at: datetime
    finished_at: datetime
    duration_ms: int = Field(ge=0)
    data: dict[str, Any] | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    error: StructuredError | None = None

    @model_validator(mode="after")
    def validate_outcome(self) -> ToolResult:
        if self.ok and self.error is not None:
            raise ValueError("successful tool results cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed tool results must contain an error")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self


class ToolDefinition(FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=240)
    arguments_schema: dict[str, Any]
    result_schema: dict[str, Any]
    risk_level: RiskLevel
    read_only: bool
    confirmation: ConfirmationMode = ConfirmationMode.NEVER
    default_timeout_seconds: float = Field(gt=0, le=3600)
    available: bool = True
    unavailable_reason: str | None = None


class VerificationRule(FrozenModel):
    predicate: str = Field(min_length=1)
    expected: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=5, gt=0, le=300)


class VerificationResult(FrozenModel):
    rule: VerificationRule
    status: VerificationStatus
    observed: dict[str, Any] = Field(default_factory=dict)
    evidence: dict[str, Any] = Field(default_factory=dict)
    message: str = ""


class NativeFunctionCall(FrozenModel):
    name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)


class NativeToolCall(FrozenModel):
    id: str | None = None
    type: Literal["function"] = "function"
    function: NativeFunctionCall


class ChatMessage(FrozenModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[NativeToolCall] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message(self) -> ChatMessage:
        if self.role == "tool" and not self.name:
            raise ValueError("tool messages require the tool name")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("only assistant messages may contain tool calls")
        return self


class NativeFunctionDefinition(FrozenModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=240)
    parameters: dict[str, Any]


class NativeToolDefinition(FrozenModel):
    type: Literal["function"] = "function"
    function: NativeFunctionDefinition


class ProviderChatResponse(FrozenModel):
    message: ChatMessage
    metadata: dict[str, Any] = Field(default_factory=dict)


class ChatRequestSettings(FrozenModel):
    temperature: float = Field(default=0.1, ge=0, le=2)
    think: bool = False
    max_output_tokens: int = Field(default=256, ge=32, le=4_096)


class PendingConfirmation(FrozenModel):
    action_id: UUID
    step_id: UUID
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any]
    provider_call_id: str | None = None
    prompt: str = Field(min_length=1)
    expires_at: datetime
    call_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class Rect(FrozenModel):
    left: int
    top: int
    right: int
    bottom: int


class WindowInfo(FrozenModel):
    handle: int
    title: str
    process_id: int = Field(ge=0)
    application: str | None = None
    rectangle: Rect | None = None
    monitor_id: str | None = None
    minimized: bool = False
    maximized: bool = False


class ProcessInfo(FrozenModel):
    process_id: int = Field(ge=0)
    name: str
    executable: str | None = None
    username: str | None = None


class MonitorInfo(FrozenModel):
    monitor_id: str
    device_name: str
    rectangle: Rect
    work_area: Rect
    primary: bool = False
    number: int | None = Field(default=None, ge=1)
    label: str | None = None
    friendly_name: str | None = None
    relative_position: str | None = None


class MonitorDestination(FrozenModel):
    relation: (
        Literal[
            "other",
            "primary",
            "left",
            "right",
            "above",
            "below",
            "nearest",
            "previous",
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Spatial destination resolved from the live Windows display arrangement. "
            "Use previous to return this window to its last monitor."
        ),
    )
    number: int | None = Field(
        default=None,
        ge=1,
        le=32,
        description="Windows display number shown in Display Settings.",
    )
    device_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        description="Exact active Windows display device or friendly monitor name.",
    )

    @model_validator(mode="after")
    def require_one_selector(self) -> MonitorDestination:
        selectors = [
            self.relation is not None,
            self.number is not None,
            self.device_name is not None,
        ]
        if sum(selectors) != 1:
            raise ValueError("provide exactly one monitor destination selector")
        return self


class WindowMoveOutcome(FrozenModel):
    verified: bool
    changed: bool
    destination: MonitorDestination
    source_monitor: MonitorInfo
    target_monitor: MonitorInfo
    observed_monitor: MonitorInfo | None = None
    preserved_state: Literal["normal", "minimized", "maximized"] = "normal"


class DesktopPerception(FrozenModel):
    captured_at: datetime = Field(default_factory=utc_now)
    foreground_window: WindowInfo | None = None
    controls: list[dict[str, Any]] = Field(default_factory=list)
    visible_text: list[str] = Field(default_factory=list)
    progress_controls: list[dict[str, Any]] = Field(default_factory=list)
    dialogs: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    screenshot_evidence: dict[str, Any] | None = None


class SceneSource(FrozenModel):
    name: str = Field(min_length=1)
    observed_at: datetime = Field(default_factory=utc_now)
    fresh_for_seconds: float = Field(default=30, gt=0, le=600)
    confidence: float = Field(default=1.0, ge=0, le=1)


class SceneElement(FrozenModel):
    label: str = Field(min_length=1, max_length=500)
    kind: str = Field(min_length=1, max_length=100)
    state: str | None = Field(default=None, max_length=200)
    source: str = Field(min_length=1)


class SceneBrowserTab(FrozenModel):
    index: int = Field(ge=1)
    active: bool = False
    title: str = Field(max_length=500)
    url: str = Field(max_length=2_048)


class BrowserScene(FrozenModel):
    running: bool = False
    title: str | None = Field(default=None, max_length=500)
    active_url: str | None = Field(default=None, max_length=2_048)
    tabs: list[SceneBrowserTab] = Field(default_factory=list, max_length=50)


class SceneChange(FrozenModel):
    kind: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=1_000)
    observed_at: datetime = Field(default_factory=utc_now)


class DesktopScene(FrozenModel):
    captured_at: datetime = Field(default_factory=utc_now)
    foreground_window: WindowInfo | None = None
    windows: list[WindowInfo] = Field(default_factory=list, max_length=200)
    browser: BrowserScene | None = None
    visual_summary: str | None = Field(default=None, max_length=2_000)
    visible_text: list[str] = Field(default_factory=list, max_length=40)
    elements: list[SceneElement] = Field(default_factory=list, max_length=80)
    dialogs: list[str] = Field(default_factory=list, max_length=20)
    redacted_content: bool = False
    sources: list[SceneSource] = Field(default_factory=list, max_length=20)
    recent_changes: list[SceneChange] = Field(default_factory=list, max_length=20)


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_user_goal: str | None = None
    active_task: str | None = None
    recent_user_messages: list[str] = Field(default_factory=list)
    recent_assistant_responses: list[str] = Field(default_factory=list)
    recent_transcript: list[dict[str, Any]] = Field(default_factory=list)
    recently_mentioned_applications: list[str] = Field(default_factory=list)
    recently_mentioned_files: list[str] = Field(default_factory=list)
    recently_mentioned_websites: list[str] = Field(default_factory=list)
    recent_audio_targets: list[dict[str, Any]] = Field(default_factory=list)
    recently_referenced_windows: list[WindowInfo] = Field(default_factory=list)
    recent_tool_results: list[ToolResult] = Field(default_factory=list)
    recent_user_corrections: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    pending_confirmations: list[PendingConfirmation] = Field(default_factory=list)
    model_messages: list[ChatMessage] = Field(default_factory=list)
    completed_tasks: list[str] = Field(default_factory=list)
    cancelled_tasks: list[str] = Field(default_factory=list)
    last_action: dict[str, Any] | None = None
    pending_offer: str | None = None
    remembered_facts: list[str] = Field(default_factory=list)


class WorldStateSnapshot(FrozenModel):
    captured_at: datetime = Field(default_factory=utc_now)
    revision: int = Field(default=0, ge=0)
    foreground_window: WindowInfo | None = None
    known_open_windows: list[WindowInfo] = Field(default_factory=list)
    monitor_layout: list[dict[str, Any]] = Field(default_factory=list)
    focus_history: list[WindowInfo] = Field(default_factory=list)
    last_desktop_perception: DesktopPerception | None = None
    desktop_scene: DesktopScene = Field(default_factory=DesktopScene)
    active_task: str | None = None
    pending_confirmation: PendingConfirmation | None = None
    recent_tool_calls: list[ToolResult] = Field(default_factory=list)
    recent_errors: list[StructuredError] = Field(default_factory=list)
    recent_verification_results: list[VerificationResult] = Field(default_factory=list)
    operating_mode: str = "text"


class EventRecord(FrozenModel):
    event_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=utc_now)
    kind: EventKind
    action_id: UUID
    step_id: UUID | None = None
    tool_name: str | None = None
    success: bool | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    error: StructuredError | None = None


class MemoryRecord(FrozenModel):
    memory_id: UUID = Field(default_factory=uuid4)
    category: str = Field(min_length=1)
    content: dict[str, Any]
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    source: str = Field(min_length=1)
    sensitivity: str = "normal"
    confidence: float = Field(ge=0, le=1)
    consent_status: ConsentStatus
