"""Tracked semantic contract for the Kimi REST and WebSocket surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


KIMI_SEMANTIC_CONTRACT_VERSION = 1
KIMI_OPENAPI_TITLE = "Kimi Code Server API"
KIMI_ASYNCAPI_TITLE = "Kimi Code WebSocket API"
KIMI_WEBSOCKET_PATH = "/api/v1/ws"
KIMI_OPENAPI_PATH = "/openapi.json"
KIMI_ASYNCAPI_PATH = "/asyncapi.json"
KIMI_REQUIRED_WEB_FLAGS = frozenset({"--no-open", "--host", "--port"})


@dataclass(frozen=True, slots=True)
class SchemaFieldContract:
    """One response/message field consumed by the bridge."""

    path: tuple[str, ...]
    types: tuple[str, ...]
    required: bool = True
    values: tuple[Any, ...] = ()


@dataclass(frozen=True, slots=True)
class RestOperationContract:
    """One REST operation used directly by :class:`KimiServerClient`."""

    name: str
    source: str
    method: str
    runtime_path: str
    spec_path: str
    request_examples: tuple[Any, ...] = ()
    query_examples: tuple[Mapping[str, Any], ...] = ()
    response_fields: tuple[SchemaFieldContract, ...] = ()
    schema_alias_note: str | None = None


@dataclass(frozen=True, slots=True)
class WebSocketMessageContract:
    """One WebSocket message shape sent or consumed by the client."""

    name: str
    source: str
    fields: tuple[SchemaFieldContract, ...]
    examples: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class SessionEventContract:
    """One event payload whose fields affect router/client behavior."""

    event_type: str
    source: str
    fields: tuple[SchemaFieldContract, ...] = ()


@dataclass(frozen=True, slots=True)
class KimiContractCheck:
    """Deterministic result for one semantic contract assertion."""

    id: str
    category: str
    status: Literal["pass", "fail"]
    detail: str
    source: str


@dataclass(frozen=True, slots=True)
class KimiCompatibilityProbe:
    """Credential-free live probe output, including artifact documents."""

    product: str
    version: str
    checks: tuple[KimiContractCheck, ...]
    openapi: dict[str, Any]
    asyncapi: dict[str, Any]


def _field(
    path: str,
    *types: str,
    required: bool = True,
    values: tuple[Any, ...] = (),
) -> SchemaFieldContract:
    return SchemaFieldContract(
        tuple(path.split(".")), types, required, values
    )


_SESSION_FIELDS = (
    _field("id", "string"),
    _field("title", "string"),
    _field("updated_at", "any"),
    _field("busy", "boolean"),
    _field("metadata.cwd", "string"),
    _field("agent_config.model", "string"),
    _field(
        "agent_config.permission_mode",
        "string",
        required=False,
        values=("manual", "auto", "yolo"),
    ),
)
_SESSION_LIST_FIELDS = tuple(
    SchemaFieldContract(
        ("items", "[]", *item.path),
        item.types,
        item.required,
        item.values,
    )
    for item in _SESSION_FIELDS
)
_PROFILE_FIELDS = (
    _field("id", "string"),
    _field("title", "string"),
    _field("busy", "boolean"),
    _field(
        "pending_interaction",
        "string",
        required=False,
        values=("none", "approval", "question"),
    ),
    _field("metadata.cwd", "string"),
    _field("agent_config.model", "string"),
    _field("agent_config.thinking", "string", required=False),
    _field(
        "agent_config.permission_mode",
        "string",
        required=False,
        values=("manual", "auto", "yolo"),
    ),
    _field("agent_config.plan_mode", "boolean", required=False),
    _field("usage", "object"),
    _field("usage.input_tokens", "integer", required=False),
    _field("usage.output_tokens", "integer", required=False),
    _field("usage.cache_read_tokens", "integer", required=False),
    _field("usage.cache_creation_tokens", "integer", required=False),
    _field("usage.context_tokens", "integer", required=False),
    _field("usage.context_limit", "integer", required=False),
)
_STATUS_FIELDS = (
    _field("busy", "boolean"),
    _field("model", "string", required=False),
    _field("thinking_level", "string"),
    _field("permission", "string", values=("manual", "auto", "yolo")),
    _field("plan_mode", "boolean"),
    _field("swarm_mode", "boolean"),
    _field("context_tokens", "integer"),
    _field("max_context_tokens", "integer"),
    _field("context_usage", "number", "integer"),
)
_TASK_FIELDS = (
    _field("id", "string"),
    _field("session_id", "string"),
    _field("kind", "string", values=("subagent", "bash", "tool")),
    _field("description", "string"),
    _field(
        "status",
        "string",
        values=("running", "completed", "failed", "cancelled"),
    ),
    _field("command", "string", required=False),
    _field("created_at", "any"),
    _field("started_at", "any", required=False),
    _field("completed_at", "any", required=False),
    _field("output_preview", "string", required=False),
    _field("output_bytes", "integer", required=False),
)


KIMI_REST_OPERATIONS: dict[str, RestOperationContract] = {
    item.name: item
    for item in (
        RestOperationContract(
            "meta",
            "KimiServerClient.meta/get_server_version",
            "GET",
            "/meta",
            "/api/v1/meta",
            response_fields=(_field("server_version", "string"),),
        ),
        RestOperationContract(
            "config",
            "KimiServerClient.get_config/get_default_model",
            "GET",
            "/config",
            "/api/v1/config",
            response_fields=(
                _field("default_model", "string", required=False),
            ),
        ),
        RestOperationContract(
            "models",
            "KimiServerClient.list_models",
            "GET",
            "/models",
            "/api/v1/models",
            response_fields=(
                _field("items", "array"),
                _field("items.[].provider", "string"),
                _field("items.[].model", "string"),
                _field("items.[].display_name", "string", required=False),
                _field("items.[].max_context_size", "integer"),
                _field("items.[].capabilities", "array", required=False),
                _field("items.[].capabilities.[]", "string", required=False),
                _field("items.[].support_efforts", "array", required=False),
                _field("items.[].support_efforts.[]", "string", required=False),
                _field("items.[].default_effort", "string", required=False),
            ),
        ),
        RestOperationContract(
            "create_session",
            "KimiServerClient.create_session",
            "POST",
            "/sessions",
            "/api/v1/sessions",
            request_examples=(
                {
                    "metadata": {"cwd": "/tmp/workspace"},
                    "title": "Compatibility probe",
                    "agent_config": {
                        "model": "kimi-code/k3",
                        "thinking": "high",
                        "permission_mode": "manual",
                        "plan_mode": False,
                    },
                },
            ),
            response_fields=(_field("id", "string"),),
        ),
        RestOperationContract(
            "list_sessions",
            "KimiServerClient.list_sessions",
            "GET",
            "/sessions",
            "/api/v1/sessions",
            query_examples=(
                {
                    "busy": False,
                    "include_archive": False,
                    "exclude_empty": False,
                    "archived_only": False,
                    "page_size": 10,
                    "before_id": "session_before",
                    "after_id": "session_after",
                },
            ),
            response_fields=(
                _field("items", "array"),
                *_SESSION_LIST_FIELDS,
            ),
        ),
        RestOperationContract(
            "get_session",
            "KimiServerClient.get_session",
            "GET",
            "/sessions/{session_id}",
            "/api/v1/sessions/{session_id}",
            response_fields=_SESSION_FIELDS,
        ),
        RestOperationContract(
            "get_profile",
            "KimiServerClient.get_session_profile",
            "GET",
            "/sessions/{session_id}/profile",
            "/api/v1/sessions/{session_id}/profile",
            response_fields=_PROFILE_FIELDS,
        ),
        RestOperationContract(
            "update_profile",
            "KimiServerClient.update_profile",
            "POST",
            "/sessions/{session_id}/profile",
            "/api/v1/sessions/{session_id}/profile",
            request_examples=(
                {
                    "title": "Renamed",
                    "agent_config": {
                        "model": "kimi-code/k3",
                        "thinking": "high",
                        "permission_mode": "yolo",
                        "plan_mode": True,
                        "goal_objective": "Finish the task",
                        "goal_control": "pause",
                    },
                },
            ),
            response_fields=_PROFILE_FIELDS,
        ),
        RestOperationContract(
            "session_status",
            "KimiServerClient.get_session_status/_materialize_session",
            "GET",
            "/sessions/{session_id}/status",
            "/api/v1/sessions/{session_id}/status",
            response_fields=_STATUS_FIELDS,
        ),
        RestOperationContract(
            "compact_session",
            "KimiServerClient.compact_session",
            "POST",
            "/sessions/{session_id}:compact",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            schema_alias_note=(
                "0.28.x OpenAPI aliases generic session actions under the "
                "archive path; the runtime path remains :compact"
            ),
        ),
        RestOperationContract(
            "undo_session",
            "KimiServerClient.undo_session",
            "POST",
            "/sessions/{session_id}:undo",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({"count": 2},),
            schema_alias_note=(
                "0.28.x OpenAPI aliases generic session actions under the "
                "archive path; the runtime path remains :undo"
            ),
        ),
        RestOperationContract(
            "goal",
            "KimiServerClient.get_goal",
            "GET",
            "/sessions/{session_id}/goal",
            "/api/v1/sessions/{session_id}/goal",
            response_fields=(
                _field("goalId", "string"),
                _field("objective", "string"),
                _field("completionCriterion", "string", required=False),
                _field(
                    "status",
                    "string",
                    values=("active", "paused", "blocked", "complete"),
                ),
                _field("turnsUsed", "number", "integer"),
                _field("tokensUsed", "number", "integer"),
                _field("wallClockMs", "number", "integer"),
                _field("budget", "object"),
                _field("budget.tokenBudget", "number", "integer"),
                _field("budget.turnBudget", "number", "integer"),
                _field("budget.wallClockBudgetMs", "number", "integer"),
                _field("budget.remainingTokens", "number", "integer"),
                _field("budget.remainingTurns", "number", "integer"),
                _field("budget.remainingWallClockMs", "number", "integer"),
                _field("budget.tokenBudgetReached", "boolean"),
                _field("budget.turnBudgetReached", "boolean"),
                _field("budget.wallClockBudgetReached", "boolean"),
                _field("budget.overBudget", "boolean"),
                _field("terminalReason", "string", required=False),
            ),
        ),
        RestOperationContract(
            "submit_prompt",
            "KimiServerClient.submit_prompt",
            "POST",
            "/sessions/{session_id}/prompts",
            "/api/v1/sessions/{session_id}/prompts",
            request_examples=(
                {
                    "content": [{"type": "text", "text": "hello"}],
                    "model": "kimi-code/k3",
                    "thinking": "high",
                    "permission_mode": "auto",
                    "plan_mode": False,
                },
                {
                    "content": [
                        {"type": "text", "text": "look"},
                        {
                            "type": "image",
                            "source": {
                                "kind": "base64",
                                "media_type": "image/png",
                                "data": "aW1hZ2U=",
                            },
                        },
                    ]
                },
            ),
            response_fields=(_field("prompt_id", "string"),),
        ),
        RestOperationContract(
            "steer_prompts",
            "KimiServerClient.steer_prompts",
            "POST",
            "/sessions/{session_id}/prompts:steer",
            "/api/v1/sessions/{session_id}/prompts:steer",
            request_examples=({"prompt_ids": ["prompt-1"]},),
            response_fields=(_field("steered", "boolean"),),
        ),
        RestOperationContract(
            "list_tasks",
            "KimiServerClient.list_tasks",
            "GET",
            "/sessions/{session_id}/tasks",
            "/api/v1/sessions/{session_id}/tasks",
            query_examples=({"status": "running"},),
            response_fields=(
                _field("items", "array"),
                *tuple(
                    SchemaFieldContract(
                        ("items", "[]", *item.path),
                        item.types,
                        item.required,
                        item.values,
                    )
                    for item in _TASK_FIELDS
                ),
            ),
        ),
        RestOperationContract(
            "get_task",
            "KimiServerClient.get_task",
            "GET",
            "/sessions/{session_id}/tasks/{task_id}",
            "/api/v1/sessions/{session_id}/tasks/{task_id}",
            query_examples=({"with_output": True, "output_bytes": 8192},),
            response_fields=_TASK_FIELDS,
        ),
        RestOperationContract(
            "cancel_task",
            "KimiServerClient.cancel_task",
            "POST",
            "/sessions/{session_id}/tasks/{task_id}:cancel",
            "/api/v1/sessions/{session_id}/tasks/{tail}",
            response_fields=(_field("cancelled", "boolean"),),
        ),
        RestOperationContract(
            "list_skills",
            "KimiServerClient.list_skills",
            "GET",
            "/sessions/{session_id}/skills",
            "/api/v1/sessions/{session_id}/skills",
            response_fields=(
                _field("skills", "array"),
                _field("skills.[].name", "string"),
                _field("skills.[].description", "string"),
                _field(
                    "skills.[].source",
                    "string",
                    values=("project", "user", "extra", "builtin"),
                ),
                _field("skills.[].path", "string"),
                _field("skills.[].type", "string", required=False),
                _field(
                    "skills.[].disable_model_invocation",
                    "boolean",
                    required=False,
                ),
            ),
        ),
        RestOperationContract(
            "activate_skill",
            "KimiServerClient.activate_skill",
            "POST",
            "/sessions/{session_id}/skills/{skill_name}:activate",
            "/api/v1/sessions/{session_id}/skills/{tail}",
            request_examples=({"args": "focus tests"}, {}),
            response_fields=(
                _field("activated", "boolean"),
                _field("skill_name", "string"),
            ),
        ),
        RestOperationContract(
            "list_tools",
            "KimiServerClient.list_tools",
            "GET",
            "/tools",
            "/api/v1/tools",
            query_examples=({"session_id": "session-1"},),
            response_fields=(
                _field("tools", "array"),
                _field("tools.[].name", "string"),
                _field("tools.[].description", "string"),
                _field(
                    "tools.[].source",
                    "string",
                    values=("builtin", "skill", "mcp"),
                ),
                _field("tools.[].mcp_server_id", "string", required=False),
            ),
        ),
        RestOperationContract(
            "list_approvals",
            "KimiServerClient.list_approvals",
            "GET",
            "/sessions/{session_id}/approvals",
            "/api/v1/sessions/{session_id}/approvals",
            query_examples=({"status": "pending"},),
            response_fields=(
                _field("items", "array"),
                _field("items.[].approval_id", "string"),
                _field("items.[].session_id", "string"),
                _field("items.[].tool_name", "string"),
                _field("items.[].action", "string", required=False),
                _field("items.[].tool_input_display", "any", required=False),
            ),
        ),
        RestOperationContract(
            "resolve_approval",
            "KimiServerClient.resolve_approval",
            "POST",
            "/sessions/{session_id}/approvals/{approval_id}",
            "/api/v1/sessions/{session_id}/approvals/{approval_id}",
            request_examples=(
                {"decision": "approved"},
                {"decision": "rejected"},
            ),
            response_fields=(_field("resolved", "boolean"),),
        ),
        RestOperationContract(
            "list_questions",
            "KimiServerClient.list_questions",
            "GET",
            "/sessions/{session_id}/questions",
            "/api/v1/sessions/{session_id}/questions",
            query_examples=({"status": "pending"},),
            response_fields=(
                _field("items", "array"),
                _field("items.[].question_id", "string"),
                _field("items.[].session_id", "string"),
                _field("items.[].questions", "array"),
                _field("items.[].questions.[].id", "string"),
                _field("items.[].questions.[].question", "string"),
                _field("items.[].questions.[].header", "string", required=False),
                _field("items.[].questions.[].body", "string", required=False),
                _field("items.[].questions.[].options", "array"),
                _field("items.[].questions.[].options.[].id", "string"),
                _field("items.[].questions.[].options.[].label", "string"),
                _field(
                    "items.[].questions.[].options.[].description",
                    "string",
                    required=False,
                ),
                _field(
                    "items.[].questions.[].multi_select",
                    "boolean",
                    required=False,
                ),
                _field(
                    "items.[].questions.[].allow_other",
                    "boolean",
                    required=False,
                ),
                _field(
                    "items.[].questions.[].other_label",
                    "string",
                    required=False,
                ),
            ),
        ),
        RestOperationContract(
            "resolve_question",
            "KimiServerClient.resolve_question",
            "POST",
            "/sessions/{session_id}/questions/{question_id}",
            "/api/v1/sessions/{session_id}/questions/{tail}",
            request_examples=(
                {
                    "answers": {
                        "q1": {"kind": "single", "option_id": "one"},
                        "q2": {"kind": "multi", "option_ids": ["a", "b"]},
                        "q3": {"kind": "other", "text": "custom"},
                        "q4": {
                            "kind": "multi_with_other",
                            "option_ids": ["a"],
                            "other_text": "custom",
                        },
                        "q5": {"kind": "skipped"},
                    },
                    "method": "click",
                },
            ),
            response_fields=(_field("resolved", "boolean"),),
        ),
        RestOperationContract(
            "dismiss_question",
            "KimiServerClient.dismiss_question",
            "POST",
            "/sessions/{session_id}/questions/{question_id}:dismiss",
            "/api/v1/sessions/{session_id}/questions/{tail}",
            response_fields=(_field("dismissed", "boolean"),),
        ),
        RestOperationContract(
            "list_prompts",
            "KimiServerClient.abort_prompt",
            "GET",
            "/sessions/{session_id}/prompts",
            "/api/v1/sessions/{session_id}/prompts",
            response_fields=(
                _field("active", "object", required=False),
                _field("active.prompt_id", "string", required=False),
                _field("queued", "array"),
            ),
        ),
        RestOperationContract(
            "abort_prompt",
            "KimiServerClient.abort_prompt",
            "POST",
            "/sessions/{session_id}/prompts/{prompt_id}:abort",
            "/api/v1/sessions/{session_id}/prompts/{tail}",
            response_fields=(_field("aborted", "boolean"),),
        ),
        RestOperationContract(
            "snapshot",
            "KimiServerClient.get_snapshot/_snapshot_resync",
            "GET",
            "/sessions/{session_id}/snapshot",
            "/api/v1/sessions/{session_id}/snapshot",
            response_fields=(
                _field("as_of_seq", "integer"),
                _field("epoch", "string"),
                _field("messages.items", "array"),
                _field("messages.items.[].role", "string"),
                _field("messages.items.[].content", "array"),
                _field(
                    "messages.items.[].content.[].text",
                    "string",
                    required=False,
                ),
                _field(
                    "messages.items.[].content.[].thinking",
                    "string",
                    required=False,
                ),
                _field("in_flight_turn", "object"),
                _field(
                    "in_flight_turn.assistant_text", "string", required=False
                ),
                _field(
                    "in_flight_turn.thinking_text", "string", required=False
                ),
            ),
        ),
    )
}


KIMI_WEBSOCKET_MESSAGES: tuple[WebSocketMessageContract, ...] = (
    WebSocketMessageContract(
        "client_hello",
        "KimiServerClient._send_client_hello",
        (
            _field("type", "string", values=("client_hello",)),
            _field("id", "string"),
            _field("payload.client_id", "string"),
            _field("payload.subscriptions", "array"),
        ),
        (
            {
                "type": "client_hello",
                "id": "request-1",
                "payload": {"client_id": "kimi-bridge", "subscriptions": []},
            },
        ),
    ),
    WebSocketMessageContract(
        "client_hello_ack",
        "KimiServerClient._wait_for_ack",
        (
            _field("type", "string", values=("ack",)),
            _field("id", "string"),
            _field("code", "integer", "number"),
            _field("msg", "string"),
        ),
    ),
    WebSocketMessageContract(
        "subscribe",
        "KimiServerClient._send_subscribe",
        (
            _field("type", "string", values=("subscribe",)),
            _field("id", "string"),
            _field("payload.session_ids", "array"),
            _field("payload.session_ids.[]", "string"),
            _field("payload.cursors", "object", required=False),
            _field("payload.cursors.{}.seq", "integer", required=False),
            _field("payload.cursors.{}.epoch", "string", required=False),
            _field("payload.agent_filter", "object", required=False),
            _field("payload.agent_filter.{}", "array", required=False),
            _field("payload.agent_filter.{}.[]", "string", required=False),
        ),
        (
            {
                "type": "subscribe",
                "id": "request-2",
                "payload": {
                    "session_ids": ["session-1"],
                    "cursors": {
                        "session-1": {"seq": 3, "epoch": "epoch-1"}
                    },
                    "agent_filter": {"session-1": ["main"]},
                },
            },
        ),
    ),
    WebSocketMessageContract(
        "subscribe_ack",
        "KimiServerClient._send_subscribe/subscribe_events",
        (
            _field("type", "string", values=("ack",)),
            _field("id", "string"),
            _field("code", "integer", "number"),
            _field("msg", "string"),
            _field("payload.accepted", "array"),
            _field("payload.accepted.[]", "string"),
            _field("payload.not_found", "array"),
            _field("payload.not_found.[]", "string"),
            _field("payload.resync_required", "array"),
            _field("payload.resync_required.[]", "string"),
            _field("payload.cursors", "object", required=False),
            _field("payload.cursors.{}.seq", "integer", required=False),
            _field("payload.cursors.{}.epoch", "string", required=False),
        ),
    ),
    WebSocketMessageContract(
        "server_hello",
        "KimiServerClient._expect_server_hello",
        (_field("type", "string", values=("server_hello",)),),
    ),
    WebSocketMessageContract(
        "ping",
        "KimiServerClient._expect_server_hello/_wait_for_ack/subscribe_events",
        (
            _field("type", "string", values=("ping",)),
            _field("payload.nonce", "string"),
        ),
    ),
    WebSocketMessageContract(
        "pong",
        "KimiServerClient._send_pong",
        (
            _field("type", "string", values=("pong",)),
            _field("payload.nonce", "string"),
        ),
        (
            {"type": "pong", "payload": {"nonce": "nonce-1"}},
        ),
    ),
    WebSocketMessageContract(
        "resync_required",
        "KimiServerClient.subscribe_events",
        (
            _field("type", "string", values=("resync_required",)),
            _field("payload.session_id", "string"),
            _field("payload.reason", "string"),
        ),
    ),
    WebSocketMessageContract(
        "error",
        "KimiServerClient._raise_ws_error",
        (
            _field("type", "string", values=("error",)),
            _field("payload.code", "integer", "number"),
            _field("payload.msg", "string"),
        ),
    ),
    WebSocketMessageContract(
        "session_event",
        "KimiServerClient.subscribe_events/ChatRouter.dispatch_event",
        (
            _field("seq", "integer"),
            _field("epoch", "string", required=False),
            _field("volatile", "boolean", required=False),
            _field("offset", "integer", required=False),
            _field("session_id", "string", required=False),
            _field("payload", "object"),
            _field("payload.type", "string"),
        ),
    ),
)


KIMI_SESSION_EVENTS: tuple[SessionEventContract, ...] = (
    SessionEventContract("turn.started", "ChatRouter.dispatch_event"),
    SessionEventContract(
        "turn.step.started",
        "ChatRouter.dispatch_event",
        (_field("step", "integer", "number"),),
    ),
    SessionEventContract(
        "assistant.delta",
        "ChatRouter.dispatch_event",
        (_field("delta", "string"),),
    ),
    SessionEventContract(
        "thinking.delta",
        "ChatRouter.dispatch_event",
        (_field("delta", "string"),),
    ),
    SessionEventContract(
        "turn.ended", "ChatRouter.dispatch_event/scripts.smoke_server"
    ),
    SessionEventContract(
        "agent.status.updated",
        "KimiServerClient._record_session_usage",
        (
            _field("usage", "object", required=False),
            _field("usage.total", "object", required=False),
            _field("usage.total.inputOther", "integer", "number", required=False),
            _field("usage.total.output", "integer", "number", required=False),
            _field(
                "usage.total.inputCacheRead",
                "integer",
                "number",
                required=False,
            ),
            _field(
                "usage.total.inputCacheCreation",
                "integer",
                "number",
                required=False,
            ),
        ),
    ),
    SessionEventContract(
        "compaction.started",
        "ChatRouter._dispatch_compaction_event",
        (_field("trigger", "string", values=("manual", "auto")),),
    ),
    SessionEventContract(
        "compaction.completed",
        "ChatRouter._dispatch_compaction_event",
        (
            _field("result", "object"),
            _field("result.compactedCount", "integer", "number"),
            _field("result.tokensBefore", "integer", "number"),
            _field("result.tokensAfter", "integer", "number"),
        ),
    ),
    SessionEventContract(
        "compaction.blocked", "ChatRouter._dispatch_compaction_event"
    ),
    SessionEventContract(
        "compaction.cancelled", "ChatRouter._dispatch_compaction_event"
    ),
)


KIMI_LIFECYCLE_INVARIANTS: tuple[tuple[str, str], ...] = (
    ("startup.foreground", "KimiServerSupervisor._run_child"),
    ("startup.bearer_token", "parse_server_startup_line"),
    ("rest.bearer_auth", "KimiServerClient._request"),
    ("session.create", "KimiServerClient.create_session"),
    ("session.materialize_before_subscribe", "KimiServerClient._materialize_session"),
    ("websocket.bearer_auth", "KimiServerClient.subscribe_events"),
    ("websocket.subscribe_ack", "KimiServerClient._send_subscribe"),
    ("websocket.reconnect_materializes", "KimiServerClient.subscribe_events"),
)


def kimi_semantic_contract() -> dict[str, Any]:
    """Return the stable, machine-readable contract consumed by automation."""

    def field_payload(item: SchemaFieldContract) -> dict[str, Any]:
        return {
            "path": list(item.path),
            "types": list(item.types),
            "required": item.required,
            "values": list(item.values),
        }

    return {
        "schema_version": KIMI_SEMANTIC_CONTRACT_VERSION,
        "cli": {
            "product": "kimi-code",
            "identity_source": "identify_kimi_executable",
            "web_command": [
                "kimi",
                "web",
                "--no-open",
                "--host",
                "127.0.0.1",
                "--port",
                "<port>",
            ],
            "required_web_flags": sorted(KIMI_REQUIRED_WEB_FLAGS),
        },
        "documents": {
            "openapi": {
                "path": KIMI_OPENAPI_PATH,
                "title": KIMI_OPENAPI_TITLE,
            },
            "asyncapi": {
                "path": KIMI_ASYNCAPI_PATH,
                "title": KIMI_ASYNCAPI_TITLE,
            },
        },
        "rest": [
            {
                "name": item.name,
                "source": item.source,
                "method": item.method,
                "runtime_path": item.runtime_path,
                "spec_path": item.spec_path,
                "request_examples": list(item.request_examples),
                "query_examples": [dict(value) for value in item.query_examples],
                "response_fields": [
                    field_payload(field) for field in item.response_fields
                ],
                "schema_alias_note": item.schema_alias_note,
            }
            for item in KIMI_REST_OPERATIONS.values()
        ],
        "websocket": {
            "path": KIMI_WEBSOCKET_PATH,
            "authentication": "Authorization: Bearer <startup-token>",
            "messages": [
                {
                    "name": item.name,
                    "source": item.source,
                    "fields": [field_payload(field) for field in item.fields],
                    "examples": [dict(value) for value in item.examples],
                }
                for item in KIMI_WEBSOCKET_MESSAGES
            ],
            "events": [
                {
                    "type": item.event_type,
                    "source": item.source,
                    "fields": [field_payload(field) for field in item.fields],
                }
                for item in KIMI_SESSION_EVENTS
            ],
        },
        "lifecycle": [
            {"id": identifier, "source": source}
            for identifier, source in KIMI_LIFECYCLE_INVARIANTS
        ],
    }


def evaluate_kimi_semantic_contract(
    openapi: Mapping[str, Any],
    asyncapi: Mapping[str, Any],
    *,
    expected_version: str | None = None,
) -> tuple[KimiContractCheck, ...]:
    """Project full upstream specs onto only the bridge's consumed contract."""

    from .contract_validation import evaluate_kimi_semantic_contract as evaluate

    return evaluate(
        openapi,
        asyncapi,
        expected_version=expected_version,
    )
