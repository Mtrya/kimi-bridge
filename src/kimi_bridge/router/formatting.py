"""Pure text formatting and event-snapshot helpers for the router."""

from __future__ import annotations

from typing import Any

from ..kimi_server import (
    GoalInfo,
    KimiServerProtocolError,
    ModelInfo,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    TaskStatus,
    ToolInfo,
)
from ..platforms.base import InboundInteraction, InboundMessage


SESSION_TITLE_LIMIT = 80


def _conversation_key(message: InboundMessage | InboundInteraction) -> str:
    conversation = message.conversation
    return f"{conversation.platform}:{conversation.bot_id}:{message.actor.id}"


def _title_from_message(message: InboundMessage) -> str:
    if message.text.strip():
        return _title_from_text(message.text)
    if message.files:
        return _title_from_text(f"File: {message.files[0].name}")
    return "Image message"


def _title_from_text(text: str) -> str:
    collapsed = " ".join(text.split())
    return collapsed[:SESSION_TITLE_LIMIT]


def _chunk_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("message limit must be positive")
    if not text:
        return []
    return [text[start : start + limit] for start in range(0, len(text), limit)]


def _format_sessions(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return "No sessions found."
    lines: list[str] = []
    for index, session in enumerate(sessions, 1):
        title = session.get("title") or "Untitled"
        workspace = session.get("metadata", {}).get("cwd", "unknown workspace")
        status = "busy" if session.get("busy") else "idle"
        lines.append(f"{index}. {title} [{status}]\n{workspace}\n{session['id']}")
    return "\n\n".join(lines)


def _format_models(current_model: str, models: list[ModelInfo]) -> str:
    lines = [f"Current model: {current_model}", "Available models:"]
    for model in models:
        display_name = model.display_name or "display name unavailable"
        efforts = ", ".join(_model_effort_choices(model))
        lines.append(f"- {model.alias} — {display_name} — thinking efforts: {efforts}")
    return "\n".join(lines)


def _model_effort_choices(model: ModelInfo) -> tuple[str, ...]:
    efforts = tuple(dict.fromkeys(model.support_efforts))
    always_thinking = "always_thinking" in model.capabilities
    if efforts:
        return efforts if always_thinking else ("off", *efforts)
    if always_thinking:
        return ("on",)
    if _model_supports_thinking(model):
        return ("on", "off")
    return ("off",)


def _model_supports_thinking(model: ModelInfo) -> bool:
    return bool({"thinking", "always_thinking"}.intersection(model.capabilities))


def _find_model(models: list[ModelInfo], alias: str) -> ModelInfo:
    model = next((item for item in models if item.alias == alias), None)
    if model is None:
        raise KimiServerProtocolError(
            f"session model {alias!r} is absent from the model catalog"
        )
    return model


def _effective_model(
    profile: SessionProfile, status: SessionStatus, default_model: str
) -> str:
    return status.model or profile.model or default_model


def _format_status(
    profile: SessionProfile,
    status: SessionStatus,
    permission_mode: str,
    server_version: str,
    default_model: str,
) -> str:
    return "\n".join(
        (
            f"Session: {profile.title}",
            f"ID: {profile.session_id}",
            f"Workspace: {profile.workspace}",
            f"State: {'busy' if status.busy else 'idle'}",
            f"Pending interaction: {profile.pending_interaction}",
            f"Model: {_effective_model(profile, status, default_model)}",
            f"Thinking effort: {status.thinking_effort}",
            f"Plan mode: {'on' if status.plan_mode else 'off'}",
            f"Permission mode: {permission_mode}",
            f"Kimi-code: {server_version}",
        )
    )


def _format_usage(usage: SessionUsage) -> str:
    if usage.context_tokens is None or usage.context_limit is None:
        context = "unknown"
    elif usage.context_limit <= 0:
        context = f"{usage.context_tokens}/{usage.context_limit} (unknown percentage)"
    else:
        percentage = usage.context_tokens / usage.context_limit * 100
        context = f"{usage.context_tokens}/{usage.context_limit} ({percentage:.1f}%)"
    return "\n".join(
        (
            f"Input tokens: {_optional_number(usage.input_tokens)}",
            f"Output tokens: {_optional_number(usage.output_tokens)}",
            f"Cache-read tokens: {_optional_number(usage.cache_read_tokens)}",
            f"Cache-creation tokens: {_optional_number(usage.cache_creation_tokens)}",
            f"Context: {context}",
        )
    )


def _optional_number(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _format_tasks(tasks: list[TaskInfo], status: TaskStatus | None) -> str:
    if not tasks:
        suffix = f" with status {status}" if status is not None else ""
        return f"No tasks found{suffix}."
    lines: list[str] = []
    for task in tasks:
        lines.append(f"{task.id} [{task.status}] {task.kind}\n{task.description}")
    return "\n\n".join(lines)


def _format_task_detail(task: TaskInfo) -> str:
    lines = [
        f"Task: {task.id}",
        f"Status: {task.status}",
        f"Kind: {task.kind}",
        f"Description: {task.description}",
    ]
    if task.command is not None:
        lines.append(f"Command: {task.command}")
    lines.append(f"Created: {task.created_at}")
    if task.started_at is not None:
        lines.append(f"Started: {task.started_at}")
    if task.completed_at is not None:
        lines.append(f"Completed: {task.completed_at}")
    if task.output_bytes is not None:
        lines.append(f"Output bytes: {task.output_bytes}")
    if task.output_preview is not None:
        lines.extend(("Output tail:", task.output_preview))
    else:
        lines.append("Output tail: unavailable")
    return "\n".join(lines)


def _format_goal(goal: GoalInfo | None) -> str:
    if goal is None:
        return "No active goal."
    budget = goal.budget
    lines = [
        f"Goal: {goal.objective}",
        f"Status: {goal.status}",
    ]
    if goal.completion_criterion is not None:
        lines.append(f"Completion criterion: {goal.completion_criterion}")
    lines.extend(
        (
            f"Used: {goal.turns_used} turns; {goal.tokens_used} tokens; {_format_milliseconds(goal.wall_clock_ms)}",
            "Budgets:",
            _format_goal_budget_line(
                "Tokens",
                budget.token_budget,
                budget.remaining_tokens,
                budget.token_budget_reached,
            ),
            _format_goal_budget_line(
                "Turns",
                budget.turn_budget,
                budget.remaining_turns,
                budget.turn_budget_reached,
            ),
            _format_goal_budget_line(
                "Time",
                budget.wall_clock_budget_ms,
                budget.remaining_wall_clock_ms,
                budget.wall_clock_budget_reached,
                milliseconds=True,
            ),
            f"Over budget: {'yes' if budget.over_budget else 'no'}",
        )
    )
    if goal.terminal_reason is not None:
        lines.append(f"Terminal reason: {goal.terminal_reason}")
    return "\n".join(lines)


def _format_goal_budget_line(
    label: str,
    limit: int | None,
    remaining: int | None,
    reached: bool,
    *,
    milliseconds: bool = False,
) -> str:
    if milliseconds:
        limit_text = _format_milliseconds(limit) if limit is not None else "not set"
        remaining_text = (
            _format_milliseconds(remaining) if remaining is not None else "not set"
        )
    else:
        limit_text = str(limit) if limit is not None else "not set"
        remaining_text = str(remaining) if remaining is not None else "not set"
    return (
        f"- {label}: limit {limit_text}; remaining {remaining_text}; "
        f"reached {'yes' if reached else 'no'}"
    )


def _format_milliseconds(value: int) -> str:
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:g} s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if remaining_seconds:
        return f"{int(minutes)} min {remaining_seconds:g} s"
    return f"{int(minutes)} min"


def _format_skills(skills: list[SkillInfo]) -> str:
    if not skills:
        return "No skills available."
    return "\n\n".join(
        f"{skill.name} [{skill.source}]\n{skill.description}" for skill in skills
    )


def _format_mcp_tools(tools: list[ToolInfo]) -> str:
    grouped: dict[str, list[ToolInfo]] = {}
    for tool in tools:
        if tool.source != "mcp" or not tool.mcp_server_id:
            continue
        grouped.setdefault(tool.mcp_server_id, []).append(tool)
    if not grouped:
        return "No MCP tools available for this session."
    sections: list[str] = []
    for server_id in sorted(grouped):
        lines = [server_id]
        for tool in sorted(grouped[server_id], key=lambda item: item.name):
            description = f" — {tool.description}" if tool.description else ""
            lines.append(f"- {tool.name}{description}")
        sections.append("\n".join(lines))
    return "MCP servers:\n" + "\n\n".join(sections)


def _session_recency_key(session: dict[str, Any]) -> str:
    updated_at = session.get("updated_at")
    return str(updated_at) if updated_at is not None else ""


def _in_flight_assistant_text(
    snapshot: dict[str, Any], *, turn_id: int | None = None
) -> str | None:
    in_flight = snapshot.get("in_flight_turn")
    if not isinstance(in_flight, dict) or not _matches_snapshot_turn(
        in_flight, turn_id
    ):
        return None
    text = in_flight.get("assistant_text")
    return text if isinstance(text, str) else None


def _persisted_assistant_text(
    snapshot: dict[str, Any], *, prompt_id: str | None = None
) -> str | None:
    message = _persisted_assistant_message(snapshot, prompt_id=prompt_id)
    if message is None:
        return None
    content = message.get("content", [])
    parts = [
        part.get("text", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "".join(str(part) for part in parts)


def _in_flight_thinking_text(
    snapshot: dict[str, Any], *, turn_id: int | None = None
) -> str | None:
    in_flight = snapshot.get("in_flight_turn")
    if not isinstance(in_flight, dict) or not _matches_snapshot_turn(
        in_flight, turn_id
    ):
        return None
    text = in_flight.get("thinking_text")
    return text if isinstance(text, str) else None


def _persisted_thinking_text(
    snapshot: dict[str, Any], *, prompt_id: str | None = None
) -> str | None:
    message = _persisted_assistant_message(snapshot, prompt_id=prompt_id)
    if message is None:
        return None
    content = message.get("content", [])
    parts = [
        part.get("thinking", "")
        for part in content
        if isinstance(part, dict) and part.get("type") == "thinking"
    ]
    return "".join(str(part) for part in parts) if parts else None


def _snapshot_prompt_id(
    snapshot: dict[str, Any], *, turn_id: int | None = None
) -> str | None:
    in_flight = snapshot.get("in_flight_turn")
    if not isinstance(in_flight, dict) or not _matches_snapshot_turn(
        in_flight, turn_id
    ):
        return None
    prompt_id = in_flight.get("current_prompt_id")
    return prompt_id if isinstance(prompt_id, str) and prompt_id else None


def _matches_snapshot_turn(in_flight: dict[str, Any], turn_id: int | None) -> bool:
    if turn_id is None:
        return True
    snapshot_turn_id = in_flight.get("turn_id")
    return snapshot_turn_id is None or snapshot_turn_id == turn_id


def _persisted_assistant_message(
    snapshot: dict[str, Any], *, prompt_id: str | None
) -> dict[str, Any] | None:
    messages = snapshot.get("messages")
    items = messages.get("items", []) if isinstance(messages, dict) else []
    assistant_messages = [
        message
        for message in items
        if isinstance(message, dict) and message.get("role") == "assistant"
    ]
    if prompt_id is not None:
        for message in reversed(assistant_messages):
            if message.get("prompt_id") == prompt_id:
                return message
        if any(
            isinstance(message.get("prompt_id"), str)
            for message in assistant_messages
        ):
            return None
    if assistant_messages:
        return assistant_messages[-1]
    return None
