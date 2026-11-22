"""
Purpose: Pydantic request/response DTOs and domain models for debates, compactions, users, prompts, admin settings, and chat models.
Role: schema
Patterns: structured-errors
Depends on: pydantic, typing
Exposes: ModelParticipantConfig, NarratorConfig, ModelInfo, DebateCreate, DebateUpdate, ContinueRequest, InterjectRequest, ForkRequest, MessageRecord, CompactionRecord, DebateRecord, ActiveTurnSnapshot, DebateDetail, UserRecord, AuthCredentials, UserPreferencesUpdate, AdminUserCreate, AdminUserUpdate, AdminSettingsResponse, AdminSettingsUpdate, TestConnectionRequest, CustomPromptCreate, CustomPromptUpdate, CustomPromptRecord
Tests: tests/test_prompt_builder.py, tests/test_llm_client.py, tests/test_debate_flow.py, tests/test_compaction.py, tests/test_auth_and_roles.py
Invariants: Zero I/O or database dependencies; never includes password hashes or plaintext API keys in response DTOs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

StyleType = Literal["cooperative", "adversarial", "explorative", "custom"]
ConvictionType = Literal["immovable", "firm", "balanced", "flexible", "synthesis"]
ThinkingModeType = Literal["default", "off", "on"]
SpeakerChoice = Literal["A", "B", "random"]
ResolvedSpeaker = Literal["A", "B"]
TurnSpeaker = Literal["A", "B", "narrator"]
NarratorCadenceMode = Literal["every_turn", "every_n", "random_1_to_n"]
DebateStatus = Literal["draft", "running", "paused", "limit_reached", "stopped", "error"]
MessageSender = Literal["A", "B", "narrator", "moderator"]
CompactionTrigger = Literal["auto", "manual", "context_too_small"]
UserRole = Literal["admin", "user"]
PriorityLevel = Literal["high", "normal", "low"]
SensitivityLevel = Literal["normal", "sensitive", "restricted"]


class ModelParticipantConfig(BaseModel):
    """Configuration for one debating participant (Model A or Model B)."""

    model_id: str = Field(default="", max_length=256)
    label: str = Field(default="", max_length=80)
    style: StyleType = "adversarial"
    custom_prompt: str = Field(default="", max_length=4000)
    conviction: ConvictionType = "firm"
    side: str = Field(default="", max_length=2000)
    system_prompt_override: str = Field(default="", max_length=8000)
    dialogue_only: bool = False
    no_thoughts_as_actions: bool = False
    no_self_narration: bool = False
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    max_tokens: int = Field(default=2048, ge=32, le=32768)
    seed: int | None = None
    thinking_mode: ThinkingModeType = "default"
    reasoning_effort: str | None = Field(default=None, max_length=64)

    def effective_label(self, fallback: str) -> str:
        """Return display label, falling back to model_id or slot name."""
        if self.label.strip():
            return self.label.strip()
        if self.model_id.strip():
            return self.model_id.strip()
        return fallback


class NarratorConfig(BaseModel):
    """Configuration for the optional 3rd Action Narrator model."""

    model_id: str = Field(default="", max_length=256)
    label: str = Field(default="Narrator", max_length=80)
    system_prompt: str = Field(default="", max_length=8000)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0)
    max_tokens: int = Field(default=1024, ge=32, le=32768)
    seed: int | None = None
    thinking_mode: ThinkingModeType = "default"
    reasoning_effort: str | None = Field(default=None, max_length=64)

    def effective_label(self, fallback: str = "Narrator") -> str:
        """Return narrator display label, falling back to 'Narrator'."""
        if self.label.strip():
            return self.label.strip()
        return fallback


class ModelInfo(BaseModel):
    """Normalized metadata for a chat-capable openWeightGym model."""

    id: str
    owned_by: str = "loadcoach"
    engine_id: str | None = None
    supports_streaming: bool = True
    is_adaptor: bool = False
    context_guaranteed: int | None = None
    context_max: int | None = None
    loaded_context: int | None = None
    supports_reasoning_effort: bool = False
    reasoning_effort_levels: list[str] = Field(default_factory=list)
    machines: list[str] = Field(default_factory=list)
    node_may_wake: bool = False


class DebateCreate(BaseModel):
    """Request payload for creating a new debate."""

    title: str = Field(default="Untitled Debate", min_length=1, max_length=200)
    scene_prompt: str = Field(default="", max_length=8000)
    first_speaker: SpeakerChoice = "A"
    turn_limit: int | None = Field(default=10, ge=1, le=200)
    time_limit_seconds: int | None = Field(default=None, ge=10, le=7200)
    auto_compact_threshold: float = Field(default=0.78, ge=0.3, le=0.98)
    keep_recent_turns: int = Field(default=4, ge=1, le=20)
    show_thinking: bool = True
    model_a_config: ModelParticipantConfig = Field(default_factory=ModelParticipantConfig)
    model_b_config: ModelParticipantConfig = Field(default_factory=ModelParticipantConfig)
    narrator_enabled: bool = False
    narrator_config: NarratorConfig = Field(default_factory=NarratorConfig)
    narrator_cadence_mode: NarratorCadenceMode = "every_turn"
    narrator_cadence_n: int = Field(default=1, ge=1, le=20)


class DebateUpdate(BaseModel):
    """Partial update payload for editing debate settings in draft or mid-debate."""

    title: str | None = Field(default=None, min_length=1, max_length=200)
    scene_prompt: str | None = Field(default=None, max_length=8000)
    first_speaker: SpeakerChoice | None = None
    turn_limit: int | None = Field(default=None, ge=1, le=200)
    time_limit_seconds: int | None = Field(default=None, ge=10, le=7200)
    auto_compact_threshold: float | None = Field(default=None, ge=0.3, le=0.98)
    keep_recent_turns: int | None = Field(default=None, ge=1, le=20)
    show_thinking: bool | None = None
    model_a_config: ModelParticipantConfig | None = None
    model_b_config: ModelParticipantConfig | None = None
    narrator_enabled: bool | None = None
    narrator_config: NarratorConfig | None = None
    narrator_cadence_mode: NarratorCadenceMode | None = None
    narrator_cadence_n: int | None = Field(default=None, ge=1, le=20)


class ContinueRequest(BaseModel):
    """Request payload to extend limits and resume a debate that reached its limit."""

    add_turns: int | None = Field(default=None, ge=1, le=100)
    add_seconds: int | None = Field(default=None, ge=10, le=3600)


class InterjectRequest(BaseModel):
    """Request payload for injecting a moderator message into a debate."""

    content: str = Field(..., min_length=1, max_length=4000)


class ForkRequest(BaseModel):
    """Request payload for duplicating a debate configuration."""

    swap_sides: bool = False
    title: str | None = Field(default=None, min_length=1, max_length=200)


class MessageRecord(BaseModel):
    """Persisted debate message (Model A, Model B, or Moderator)."""

    id: str
    debate_id: str
    turn_index: int
    sender: MessageSender
    model_id: str | None = None
    answered_model_id: str | None = None
    answered_node: str | None = None
    content: str
    thinking: str = ""
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    loaded_context_tokens: int | None = None
    duration_ms: int | None = None
    tokens_per_sec: float | None = None
    request_id: str | None = None
    loadcoach_request_id: str | None = None
    finish_reason: str | None = None
    created_at: str


class CompactionRecord(BaseModel):
    """Persisted context compaction summary covering messages through `through_message_id`."""

    id: str
    debate_id: str
    through_message_id: str
    summary_text: str
    summarizer_model_id: str
    trigger_reason: CompactionTrigger
    created_at: str


class DebateRecord(BaseModel):
    """Persisted debate configuration and runtime state."""

    id: str
    user_id: str | None = None
    title: str
    scene_prompt: str = ""
    first_speaker: SpeakerChoice = "A"
    resolved_first_speaker: ResolvedSpeaker | None = None
    status: DebateStatus = "draft"
    turn_limit: int | None = 10
    time_limit_seconds: int | None = None
    elapsed_active_seconds: float = 0.0
    auto_compact_threshold: float = 0.78
    keep_recent_turns: int = 4
    show_thinking: bool = True
    model_a_config: ModelParticipantConfig = Field(default_factory=ModelParticipantConfig)
    model_b_config: ModelParticipantConfig = Field(default_factory=ModelParticipantConfig)
    model_a_ctx_loaded: int | None = None
    model_a_ctx_guaranteed: int | None = None
    model_a_ctx_max: int | None = None
    model_b_ctx_loaded: int | None = None
    model_b_ctx_guaranteed: int | None = None
    model_b_ctx_max: int | None = None
    narrator_enabled: bool = False
    narrator_config: NarratorConfig = Field(default_factory=NarratorConfig)
    narrator_cadence_mode: NarratorCadenceMode = "every_turn"
    narrator_cadence_n: int = 1
    narrator_turns_until_next: int | None = None
    narrator_ctx_loaded: int | None = None
    narrator_ctx_guaranteed: int | None = None
    narrator_ctx_max: int | None = None
    last_error: str | None = None
    created_at: str
    updated_at: str
    message_count: int = 0


class ActiveTurnSnapshot(BaseModel):
    """In-progress streaming turn state used to catch up reconnecting SSE clients."""

    debate_id: str
    turn_index: int
    speaker: TurnSpeaker
    model_id: str
    thinking: str = ""
    content: str = ""
    started_at_ms: int
    status_hint: str | None = None


class DebateDetail(BaseModel):
    """Full debate response including transcript messages, compactions, and active streaming snapshot."""

    debate: DebateRecord
    messages: list[MessageRecord]
    compactions: list[CompactionRecord] = Field(default_factory=list)
    active_turn: ActiveTurnSnapshot | None = None
    global_active_debate_id: str | None = None


# --- Users, Auth, Prompts, and Admin DTOs ---


class UserRecord(BaseModel):
    """Public representation of a user account (never exposes password hash)."""

    id: str
    username: str
    role: UserRole
    theme: str = "obsidian"
    show_thinking_default: bool = True
    is_active: bool = True
    created_at: str


class AuthCredentials(BaseModel):
    """Registration or login credentials."""

    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=6, max_length=256)


class UserPreferencesUpdate(BaseModel):
    """Per-user UI preferences update payload."""

    theme: str | None = Field(default=None, max_length=32)
    show_thinking_default: bool | None = None


class AdminUserCreate(BaseModel):
    """Admin-initiated user creation payload."""

    username: str = Field(..., min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    password: str = Field(..., min_length=6, max_length=256)
    role: UserRole = "user"


class AdminUserUpdate(BaseModel):
    """Admin-initiated user modification payload."""

    role: UserRole | None = None
    is_active: bool | None = None
    password: str | None = Field(default=None, min_length=6, max_length=256)


class AdminSettingsResponse(BaseModel):
    """Safe API view of singleton app_settings (omits plaintext API key)."""

    api_protocol: Literal["http", "https"] = "http"
    api_host: str = "127.0.0.1"
    api_port: int = 8442
    api_key_configured: bool = False
    open_registration: bool = True
    suite_application: str = "ai-debate"
    default_priority: PriorityLevel | None = None
    default_sensitivity: SensitivityLevel | None = None
    default_node: str | None = None
    log_off: bool = False
    no_wake: bool = False


class AdminSettingsUpdate(BaseModel):
    """Partial update for singleton app_settings."""

    api_protocol: Literal["http", "https"] | None = None
    api_host: str | None = Field(default=None, min_length=1, max_length=255)
    api_port: int | None = Field(default=None, ge=1, le=65535)
    api_key: str | None = Field(default=None, max_length=512)
    clear_api_key: bool = False
    open_registration: bool | None = None
    suite_application: str | None = Field(default=None, min_length=1, max_length=128)
    default_priority: PriorityLevel | Literal[""] | None = None
    default_sensitivity: SensitivityLevel | Literal[""] | None = None
    default_node: str | None = Field(default=None, max_length=128)
    log_off: bool | None = None
    no_wake: bool | None = None


class TestConnectionRequest(BaseModel):
    """Optional override parameters when testing openWeightGym connectivity from Admin UI."""

    api_protocol: Literal["http", "https"] | None = None
    api_host: str | None = None
    api_port: int | None = Field(default=None, ge=1, le=65535)
    api_key: str | None = None
    suite_application: str | None = None


class CustomPromptCreate(BaseModel):
    """Payload to create a saved custom prompt preset."""

    name: str = Field(..., min_length=1, max_length=100)
    style_category: StyleType = "custom"
    content: str = Field(..., min_length=1, max_length=4000)


class CustomPromptUpdate(BaseModel):
    """Payload to update an existing saved custom prompt preset."""

    name: str | None = Field(default=None, min_length=1, max_length=100)
    style_category: StyleType | None = None
    content: str | None = Field(default=None, min_length=1, max_length=4000)


class CustomPromptRecord(BaseModel):
    """Persisted per-user custom prompt record."""

    id: str
    user_id: str
    name: str
    style_category: StyleType
    content: str
    created_at: str
    updated_at: str
