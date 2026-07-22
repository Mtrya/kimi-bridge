"""Kimi wire-shape serialization and typed response parsing."""

from __future__ import annotations

from typing import Any, cast

from ..interactions import (
    ApprovalRequest,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionAnswer,
    QuestionOption,
    QuestionRequest,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from .types import (
    GoalBudget,
    GoalInfo,
    GoalStatus,
    KimiServerProtocolError,
    ModelInfo,
    PendingInteractionKind,
    PermissionMode,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    SkillSource,
    TaskInfo,
    TaskKind,
    TaskStatus,
    ToolInfo,
    ToolSource,
)


def _model_info_from_wire(value: dict[str, Any]) -> ModelInfo:
    capabilities = value.get("capabilities") or []
    support_efforts = value.get("support_efforts") or []
    display_name = value.get("display_name")
    default_effort = value.get("default_effort")
    return ModelInfo(
        alias=str(value["model"]),
        provider=str(value["provider"]),
        display_name=(str(display_name) if display_name is not None else None),
        max_context_size=int(value["max_context_size"]),
        capabilities=tuple(str(item) for item in capabilities),
        support_efforts=tuple(str(item) for item in support_efforts),
        default_effort=(
            str(default_effort) if default_effort is not None else None
        ),
    )


def _session_usage_from_wire(value: dict[str, Any]) -> SessionUsage:
    return SessionUsage(
        input_tokens=_optional_int(value.get("input_tokens")),
        output_tokens=_optional_int(value.get("output_tokens")),
        cache_read_tokens=_optional_int(value.get("cache_read_tokens")),
        cache_creation_tokens=_optional_int(value.get("cache_creation_tokens")),
        context_tokens=_optional_int(value.get("context_tokens")),
        context_limit=_optional_int(value.get("context_limit")),
    )


def _session_profile_from_wire(value: dict[str, Any]) -> SessionProfile:
    agent_config = value["agent_config"]
    metadata = value["metadata"]
    pending_interaction = value.get("pending_interaction", "none")
    if pending_interaction not in {"none", "approval", "question"}:
        raise KimiServerProtocolError(
            f"unknown pending interaction kind: {pending_interaction!r}"
        )
    permission_mode = agent_config.get("permission_mode")
    if permission_mode is not None and permission_mode not in {
        "manual",
        "auto",
        "yolo",
    }:
        raise KimiServerProtocolError(
            f"unknown session permission mode: {permission_mode!r}"
        )
    thinking_effort = agent_config.get("thinking")
    plan_mode = agent_config.get("plan_mode")
    return SessionProfile(
        session_id=str(value["id"]),
        title=str(value["title"]),
        workspace=str(metadata["cwd"]),
        busy=bool(value["busy"]),
        pending_interaction=cast(PendingInteractionKind, pending_interaction),
        model=str(agent_config["model"]),
        thinking_effort=(
            str(thinking_effort) if thinking_effort is not None else None
        ),
        permission_mode=cast(PermissionMode | None, permission_mode),
        plan_mode=(bool(plan_mode) if plan_mode is not None else None),
        usage=_session_usage_from_wire(value["usage"]),
    )


def _session_status_from_wire(value: dict[str, Any]) -> SessionStatus:
    permission_mode = value["permission"]
    if permission_mode not in {"manual", "auto", "yolo"}:
        raise KimiServerProtocolError(
            f"unknown session permission mode: {permission_mode!r}"
        )
    model = value.get("model")
    return SessionStatus(
        busy=bool(value["busy"]),
        model=str(model) if model else None,
        thinking_effort=str(value["thinking_level"]),
        permission_mode=cast(PermissionMode, permission_mode),
        plan_mode=bool(value["plan_mode"]),
        swarm_mode=bool(value["swarm_mode"]),
        context_tokens=int(value["context_tokens"]),
        context_limit=int(value["max_context_tokens"]),
        context_usage=float(value["context_usage"]),
    )


def _goal_info_from_wire(value: dict[str, Any]) -> GoalInfo:
    status = value["status"]
    if status not in {"active", "paused", "blocked", "complete"}:
        raise KimiServerProtocolError(f"unknown goal status: {status!r}")
    budget = value["budget"]
    completion_criterion = value.get("completionCriterion")
    terminal_reason = value.get("terminalReason")
    return GoalInfo(
        id=str(value["goalId"]),
        objective=str(value["objective"]),
        completion_criterion=(
            str(completion_criterion)
            if completion_criterion is not None
            else None
        ),
        status=cast(GoalStatus, status),
        turns_used=int(value["turnsUsed"]),
        tokens_used=int(value["tokensUsed"]),
        wall_clock_ms=int(value["wallClockMs"]),
        budget=GoalBudget(
            token_budget=_optional_int(budget.get("tokenBudget")),
            turn_budget=_optional_int(budget.get("turnBudget")),
            wall_clock_budget_ms=_optional_int(
                budget.get("wallClockBudgetMs")
            ),
            remaining_tokens=_optional_int(budget.get("remainingTokens")),
            remaining_turns=_optional_int(budget.get("remainingTurns")),
            remaining_wall_clock_ms=_optional_int(
                budget.get("remainingWallClockMs")
            ),
            token_budget_reached=bool(budget["tokenBudgetReached"]),
            turn_budget_reached=bool(budget["turnBudgetReached"]),
            wall_clock_budget_reached=bool(
                budget["wallClockBudgetReached"]
            ),
            over_budget=bool(budget["overBudget"]),
        ),
        terminal_reason=(
            str(terminal_reason) if terminal_reason is not None else None
        ),
    )


def _task_info_from_wire(value: dict[str, Any]) -> TaskInfo:
    kind = value["kind"]
    status = value["status"]
    if kind not in {"subagent", "bash", "tool"}:
        raise KimiServerProtocolError(f"unknown task kind: {kind!r}")
    if status not in {"running", "completed", "failed", "cancelled"}:
        raise KimiServerProtocolError(f"unknown task status: {status!r}")
    command = value.get("command")
    output_preview = value.get("output_preview")
    return TaskInfo(
        id=str(value["id"]),
        session_id=str(value["session_id"]),
        kind=cast(TaskKind, kind),
        description=str(value["description"]),
        status=cast(TaskStatus, status),
        command=str(command) if command is not None else None,
        created_at=value["created_at"],
        started_at=value.get("started_at"),
        completed_at=value.get("completed_at"),
        output_preview=(
            str(output_preview) if output_preview is not None else None
        ),
        output_bytes=_optional_int(value.get("output_bytes")),
    )


def _skill_info_from_wire(value: dict[str, Any]) -> SkillInfo:
    source = value["source"]
    if source not in {"project", "user", "extra", "builtin"}:
        raise KimiServerProtocolError(f"unknown skill source: {source!r}")
    skill_type = value.get("type")
    disable_model_invocation = value.get("disable_model_invocation")
    return SkillInfo(
        name=str(value["name"]),
        description=str(value["description"]),
        source=cast(SkillSource, source),
        path=str(value["path"]),
        kind=str(skill_type) if skill_type is not None else None,
        disable_model_invocation=(
            bool(disable_model_invocation)
            if disable_model_invocation is not None
            else None
        ),
    )


def _tool_info_from_wire(value: dict[str, Any]) -> ToolInfo:
    source = value["source"]
    if source not in {"builtin", "skill", "mcp"}:
        raise KimiServerProtocolError(f"unknown tool source: {source!r}")
    mcp_server_id = value.get("mcp_server_id")
    return ToolInfo(
        name=str(value["name"]),
        description=str(value["description"]),
        source=cast(ToolSource, source),
        mcp_server_id=(
            str(mcp_server_id) if mcp_server_id is not None else None
        ),
    )


def _optional_int(value: Any) -> int | None:
    return int(value) if value is not None else None


def _approval_request_from_wire(value: dict[str, Any]) -> ApprovalRequest:
    action = value.get("action")
    return ApprovalRequest(
        id=str(value["approval_id"]),
        session_id=str(value["session_id"]),
        tool_name=str(value["tool_name"]),
        action=str(action) if action else "Approval required",
        input_display=value.get("tool_input_display"),
    )


def _question_request_from_wire(value: dict[str, Any]) -> QuestionRequest:
    questions: list[Question] = []
    for item in value["questions"]:
        options = tuple(
            QuestionOption(
                id=str(option["id"]),
                label=str(option["label"]),
                description=(
                    str(option["description"])
                    if option.get("description") is not None
                    else None
                ),
            )
            for option in item["options"]
        )
        questions.append(
            Question(
                id=str(item["id"]),
                text=str(item["question"]),
                options=options,
                header=(str(item["header"]) if item.get("header") else None),
                body=str(item["body"]) if item.get("body") else None,
                multi_select=bool(item.get("multi_select", False)),
                allow_other=bool(item.get("allow_other", False)),
                other_label=(
                    str(item["other_label"]) if item.get("other_label") else None
                ),
            )
        )
    return QuestionRequest(
        id=str(value["question_id"]),
        session_id=str(value["session_id"]),
        questions=tuple(questions),
    )


def _question_answer_to_wire(answer: QuestionAnswer) -> dict[str, Any]:
    if isinstance(answer, SkippedAnswer):
        return {"kind": "skipped"}
    if isinstance(answer, SingleChoiceAnswer):
        return {"kind": "single", "option_id": answer.option_id}
    if isinstance(answer, MultipleChoiceAnswer):
        return {"kind": "multi", "option_ids": list(answer.option_ids)}
    if isinstance(answer, OtherAnswer):
        return {"kind": "other", "text": answer.text}
    if isinstance(answer, MultipleChoiceWithOtherAnswer):
        return {
            "kind": "multi_with_other",
            "option_ids": list(answer.option_ids),
            "other_text": answer.text,
        }
    raise TypeError(f"unsupported question answer: {type(answer).__name__}")
