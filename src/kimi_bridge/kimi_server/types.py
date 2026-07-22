"""Public types and errors for the Kimi server boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


class KimiServerError(RuntimeError):
    """Base error for the bridge's kimi-server boundary."""


class KimiServerStartupError(KimiServerError):
    """The managed server could not become ready."""


class KimiServerAuthenticationError(KimiServerStartupError):
    """kimi-code is not authenticated on this host."""


class KimiServerAPIError(KimiServerError):
    """A REST call returned a non-zero kimi-server envelope code."""

    def __init__(
        self,
        code: int | float,
        message: str,
        *,
        request_id: str | None = None,
        details: Any = None,
    ) -> None:
        suffix = f" (request_id={request_id})" if request_id else ""
        super().__init__(f"kimi server API error {code}: {message}{suffix}")
        self.code = code
        self.message = message
        self.request_id = request_id
        self.details = details


class KimiServerProtocolError(KimiServerError):
    """The server violated or rejected the expected REST/WebSocket protocol."""


@dataclass(frozen=True, slots=True)
class ServerConnection:
    """Current endpoint for one generation of the managed child."""

    base_url: str
    port: int
    generation: int
    token: str = field(repr=False)


PermissionMode = Literal["manual", "auto", "yolo"]
PendingInteractionKind = Literal["none", "approval", "question"]
GoalControl = Literal["pause", "resume", "cancel"]
GoalStatus = Literal["active", "paused", "blocked", "complete"]
TaskStatus = Literal["running", "completed", "failed", "cancelled"]
TaskKind = Literal["subagent", "bash", "tool"]
SkillSource = Literal["project", "user", "extra", "builtin"]
ToolSource = Literal["builtin", "skill", "mcp"]


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One exact model alias advertised by the managed server."""

    alias: str
    provider: str
    display_name: str | None
    max_context_size: int
    capabilities: tuple[str, ...]
    support_efforts: tuple[str, ...]
    default_effort: str | None


@dataclass(frozen=True, slots=True)
class SessionUsage:
    """Usage available through the public server surfaces for a session."""

    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    context_tokens: int | None
    context_limit: int | None


@dataclass(frozen=True, slots=True)
class SessionProfile:
    """Session profile fields used by bridge controls and inspection."""

    session_id: str
    title: str
    workspace: str
    busy: bool
    pending_interaction: PendingInteractionKind
    model: str
    thinking_effort: str | None
    permission_mode: PermissionMode | None
    plan_mode: bool | None
    usage: SessionUsage


@dataclass(frozen=True, slots=True)
class SessionStatus:
    """Realtime status materialized by kimi-code for one session."""

    busy: bool
    model: str | None
    thinking_effort: str
    permission_mode: PermissionMode
    plan_mode: bool
    swarm_mode: bool
    context_tokens: int
    context_limit: int
    context_usage: float


@dataclass(frozen=True, slots=True)
class GoalBudget:
    """Public budget state for one Kimi goal."""

    token_budget: int | None
    turn_budget: int | None
    wall_clock_budget_ms: int | None
    remaining_tokens: int | None
    remaining_turns: int | None
    remaining_wall_clock_ms: int | None
    token_budget_reached: bool
    turn_budget_reached: bool
    wall_clock_budget_reached: bool
    over_budget: bool


@dataclass(frozen=True, slots=True)
class GoalInfo:
    """Authoritative public-v1 state for one Kimi goal."""

    id: str
    objective: str
    completion_criterion: str | None
    status: GoalStatus
    turns_used: int
    tokens_used: int
    wall_clock_ms: int
    budget: GoalBudget
    terminal_reason: str | None


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """One public background task record."""

    id: str
    session_id: str
    kind: TaskKind
    description: str
    status: TaskStatus
    command: str | None
    created_at: Any
    started_at: Any = None
    completed_at: Any = None
    output_preview: str | None = None
    output_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class SkillInfo:
    """One skill available to a bound session."""

    name: str
    description: str
    source: SkillSource
    path: str
    kind: str | None = None
    disable_model_invocation: bool | None = None


@dataclass(frozen=True, slots=True)
class ToolInfo:
    """One tool resolved for a bound session."""

    name: str
    description: str
    source: ToolSource
    mcp_server_id: str | None = None
