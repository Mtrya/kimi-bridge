"""Tracked semantic contract for the Kimi REST and WebSocket surfaces."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


KIMI_SEMANTIC_CONTRACT_VERSION = 15
KIMI_OPENAPI_TITLE = "Kimi Code Server API"
KIMI_ASYNCAPI_TITLE = "Kimi Code WebSocket API"
KIMI_WEBSOCKET_PATH = "/api/v1/ws"
KIMI_OPENAPI_PATH = "/openapi.json"
KIMI_ASYNCAPI_PATH = "/asyncapi.json"
KIMI_REQUIRED_WEB_FLAGS = frozenset({"--no-open", "--host", "--port"})


@dataclass(frozen=True, slots=True)
class SchemaFieldContract:
    """One schema field consumed by the bridge."""

    path: tuple[str, ...]
    types: tuple[str, ...]
    required: bool = True
    values: tuple[Any, ...] = ()
    format: str | None = None
    required_if_parent_present: bool = False


@dataclass(frozen=True, slots=True)
class RestOperationContract:
    """One REST operation used directly by :class:`KimiServerClient`."""

    name: str
    source: str
    method: str
    runtime_path: str
    spec_path: str
    request_media_type: str = "application/json"
    request_examples: tuple[Any, ...] = ()
    request_fields: tuple[SchemaFieldContract, ...] = ()
    query_examples: tuple[Mapping[str, Any], ...] = ()
    response_fields: tuple[SchemaFieldContract, ...] = ()
    response_media_type: str = "application/json"
    schema_alias_note: str | None = None
    api_version: Literal["v1", "v2"] = "v1"


@dataclass(frozen=True, slots=True)
class WebSocketMessageContract:
    """One WebSocket message shape sent or consumed by the client."""

    name: str
    source: str
    fields: tuple[SchemaFieldContract, ...] = ()
    examples: tuple[Mapping[str, Any], ...] = ()
    discriminator: tuple[str, str] | None = None


@dataclass(frozen=True, slots=True)
class TranscriptOpContract:
    """One transcript journal operation folded into the semantic events."""

    op: str
    source: str
    fields: tuple[SchemaFieldContract, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionEventContract:
    """One semantic event emitted by the bridge's transcript projection."""

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
    format: str | None = None,
    required_if_parent_present: bool = False,
) -> SchemaFieldContract:
    return SchemaFieldContract(
        tuple(path.split(".")),
        types,
        required,
        values,
        format,
        required_if_parent_present,
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
    _field("usage.context_tokens", "integer"),
    _field("usage.context_limit", "integer", required=False),
    _field("usage.input_tokens", "integer"),
    _field("usage.output_tokens", "integer"),
    _field("usage.cache_read_tokens", "integer"),
    _field("usage.cache_creation_tokens", "integer"),
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
_TASK_FRAME_FIELDS = (
    _field("task_id", "string"),
    _field(
        "kind",
        "string",
        values=("shell", "subagent", "other"),
    ),
    _field("status", "string", values=("running", "ended")),
    _field(
        "outcome",
        "string",
        required=False,
        values=("completed", "failed", "timed_out", "killed", "lost"),
    ),
    _field("detached", "boolean"),
    _field("child_agent_id", "string", required=False),
    _field("swarm_index", "integer", required=False),
    _field("phase", "string", required=False, values=("working", "suspended")),
    _field("model", "string", required=False),
    _field("description", "string", required=False),
    _field("command", "string", required=False),
    _field("started_at", "string", required=False),
    _field("ended_at", "string", required=False),
    _field("result_summary", "string", required=False),
    _field("error", "string", required=False),
)
_TASK_LIST_FRAME_FIELDS = tuple(
    SchemaFieldContract(
        ("tasks", "[]", *item.path),
        item.types,
        item.required,
        item.values,
    )
    for item in _TASK_FRAME_FIELDS
)
_TASK_RESPONSE_FIELDS = tuple(
    SchemaFieldContract(
        ("task", *item.path),
        item.types,
        required=False,
        values=item.values,
        required_if_parent_present=item.required,
    )
    for item in _TASK_FRAME_FIELDS
)


_GOAL_FIELDS = (
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
            "KimiServerClient.get_config/get_default_model/get_secondary_model",
            "GET",
            "/config",
            "/api/v1/config",
            response_fields=(
                _field("default_model", "string", required=False),
                _field("secondary_model", "object", required=False),
            ),
        ),
        RestOperationContract(
            "update_config",
            "KimiServerClient.set_secondary_model/set_secondary_model_entry",
            "POST",
            "/config",
            "/api/v1/config",
            request_examples=(
                {"secondary_model": {"default_model": "kimi-code/k3"}},
                {
                    "secondary_model": {
                        "default_model": "kimi-code/k3",
                        "models": {"kimi-code/k3": ""},
                    }
                },
                {"secondary_model": {"models": {"kimi-code/k3": "Hard tasks"}}},
            ),
            request_fields=(_field("secondary_model", "object", required=False),),
            response_fields=(_field("secondary_model", "object", required=False),),
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
            "upload_file",
            "KimiServerClient._upload_prompt_media",
            "POST",
            "/files",
            "/api/v1/files",
            request_media_type="multipart/form-data",
            request_examples=({"file": "binary", "name": "photo.png"},),
            request_fields=(_field("file", "string", format="binary"),),
            response_fields=(_field("id", "string"),),
        ),
        RestOperationContract(
            "create_session",
            "KimiServerClient.create_session",
            "POST",
            "/sessions",
            "/api/v1/sessions",
            request_examples=(
                {"metadata": {"cwd": "/workspace"}, "environment_id": "remote-fixture", "environment_cwd": "/target/workspace"},
                {
                    "metadata": {"cwd": "/tmp/workspace"},
                    "title": "Compatibility probe",
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
            "archive_session",
            "KimiServerClient.archive_session",
            "POST",
            "/sessions/{session_id}:archive",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            response_fields=(_field("archived", "boolean", values=(True,)),),
        ),
        RestOperationContract(
            "get_session",
            "KimiServerClient.get_session/get_session_status/get_session_usage",
            "GET",
            "/sessions/{session_id}",
            "/api/v1/sessions/{session_id}",
            response_fields=(
                *_SESSION_FIELDS,
                _field("archived", "boolean", required=False),
            ),
        ),
        RestOperationContract(
            "reload_session",
            "KimiServerClient.reload_session",
            "POST",
            "/sessions/{session_id}:reload",
            "/api/v1/sessions/{session_id}:reload",
            request_examples=({},),
        ),
        RestOperationContract("session_environment", "WorkspaceOperations.get_file_context", "GET", "/sessions/{session_id}/environment", "/api/v1/sessions/{session_id}/environment", response_fields=(_field("workspace_id", "string"), _field("environment_id", "string"), _field("cwd", "string", required=False))),
        RestOperationContract("list_workspaces", "WorkspaceOperations.get_file_context", "GET", "/workspaces", "/api/v1/workspaces", response_fields=(_field("items", "array"), _field("items.[].id", "string"), _field("items.[].root", "string"), _field("items.[].additional_dirs", "array"))),
        RestOperationContract("update_workspace", "WorkspaceOperations.change_workspace", "PATCH", "/workspaces/{workspace_id}", "/api/v1/workspaces/{workspace_id}", request_examples=({"additional_dirs": ["/workspace/additional"]},), response_fields=(_field("id", "string"), _field("root", "string"), _field("additional_dirs", "array"))),
        RestOperationContract("workspace_primary", "WorkspaceOperations.change_workspace", "POST", "/workspaces/{workspace_id}/primary", "/api/v1/workspaces/{workspace_id}/primary", request_examples=({"path": "/workspace/additional"},), response_fields=(_field("id", "string"), _field("root", "string"), _field("additional_dirs", "array"))),
        RestOperationContract("list_files", "WorkspaceOperations.list_workspace_files", "POST", "/sessions/{session_id}/fs:list", "/api/v1/sessions/{session_id}/{tail}", request_examples=({"path": ".", "depth": 1, "limit": 100, "allow_external_symlinks": False},), response_fields=(_field("items", "array"), _field("items.[].path", "string"), _field("items.[].name", "string"), _field("items.[].kind", "string"), _field("items.[].size", "integer", required=False), _field("truncated", "boolean"))),
        RestOperationContract("search_files", "WorkspaceOperations.list_workspace_files", "POST", "/sessions/{session_id}/fs:search", "/api/v1/sessions/{session_id}/{tail}", request_examples=({"query": "fixture", "limit": 50},), response_fields=(_field("items", "array"), _field("items.[].path", "string"), _field("items.[].name", "string"), _field("items.[].kind", "string"), _field("truncated", "boolean"))),
        RestOperationContract("read_file", "WorkspaceOperations.preview_workspace_file", "POST", "/sessions/{session_id}/fs:read", "/api/v1/sessions/{session_id}/{tail}", request_examples=({"path": "fixture.txt", "length": 8192, "encoding": "auto", "allow_external_symlinks": False},), response_fields=(_field("path", "string"), _field("content", "string"), _field("encoding", "string", values=("utf-8", "base64")), _field("size", "integer"), _field("is_binary", "boolean"), _field("truncated", "boolean"))),
        RestOperationContract("download_file", "WorkspaceOperations.download_workspace_file", "GET", "/sessions/{session_id}/fs/{path}:download", "/api/v1/sessions/{session_id}/fs/{*}", response_media_type="application/octet-stream"),
        RestOperationContract("session_environments", "WorkspaceOperations.list_environments", "GET", "/sessions/{session_id}/environments", "/api/v1/sessions/{session_id}/environments", response_fields=(_field("environments", "array"), _field("environments.[].environment_id", "string"), _field("environments.[].type", "string", values=("local", "ssh", "docker", "command")), _field("environments.[].status", "string", values=("pending", "connecting", "ready", "disconnected", "disposed")))),
        RestOperationContract("session_plan", "WorkspaceOperations.get_plan_document", "GET", "/sessions/{session_id}/transcript/plan", "/api/v1/sessions/{session_id}/transcript/plan", query_examples=({"agent_id": "main", "tool_call_id": "plan-review"},), response_fields=(_field("plans", "array"), _field("plans.[].tool_call_id", "string"), _field("plans.[].plan", "string"), _field("plans.[].path", "string", required=False))),
        RestOperationContract("file_changes", "FileHistoryOperations.get_file_changes", "GET", "/sessions/{session_id}/file-history/changes", "/api/v1/sessions/{session_id}/file-history/changes", query_examples=({"turn_id": 0},), response_fields=(_field("recorded", "boolean"), _field("changes", "array"), _field("changes.[].path", "string"), _field("changes.[].status", "string", values=("added", "modified", "deleted")), _field("changes.[].additions", "integer", "number"), _field("changes.[].deletions", "integer", "number"), _field("changes.[].binary", "boolean", required=False), _field("changes.[].oversize", "boolean", required=False))),
        RestOperationContract("file_change_content", "FileHistoryOperations.get_file_diff", "GET", "/sessions/{session_id}/file-history/content", "/api/v1/sessions/{session_id}/file-history/content", query_examples=({"turn_id": 0, "path": "fixture.txt", "phase": "start"}, {"turn_id": 0, "path": "fixture.txt", "phase": "end"}), response_fields=(_field("content", "object", required=False), _field("content.content", "string", required=False), _field("content.binary", "boolean", required=False))),
        RestOperationContract("list_plugins", "IntegrationOperations.list_plugins", "GET", "/plugins", "/api/v1/plugins", response_fields=(_field("plugins", "array"), _field("plugins.[].id", "string"), _field("plugins.[].displayName", "string"), _field("plugins.[].enabled", "boolean"), _field("plugins.[].state", "string", values=("ok", "error")), _field("plugins.[].version", "string", required=False), _field("plugins.[].skillCount", "number", "integer"), _field("plugins.[].mcpServerCount", "number", "integer"))),
        RestOperationContract("plugin_details", "IntegrationOperations.get_plugin_details", "GET", "/plugins/{plugin_id}", "/api/v1/plugins/{tail}", response_fields=(_field("id", "string"), _field("displayName", "string"), _field("enabled", "boolean"), _field("state", "string"), _field("skillCount", "number", "integer"), _field("mcpServerCount", "number", "integer"), _field("skills", "array"), _field("skills.[].name", "string"), _field("mcpServers", "array"), _field("mcpServers.[].name", "string"), _field("mcpServers.[].runtimeName", "string"), _field("mcpServers.[].enabled", "boolean"), _field("mcpServers.[].transport", "string"), _field("diagnostics", "array"), _field("diagnostics.[].severity", "string", values=("error", "warn", "info")))),
        RestOperationContract("mcp_servers", "IntegrationOperations.list_mcp_servers", "GET", "/mcp/servers", "/api/v1/mcp/servers", response_fields=(_field("servers", "array"), _field("servers.[].id", "string"), _field("servers.[].name", "string"), _field("servers.[].transport", "string", values=("stdio", "http", "sse")), _field("servers.[].status", "string", values=("connected", "connecting", "disconnected", "error")), _field("servers.[].tool_count", "integer"))),
        RestOperationContract("restart_mcp", "IntegrationOperations.restart_mcp_server", "POST", "/mcp/servers/{server_id}:restart", "/api/v1/mcp/servers/{tail}", response_fields=(_field("restarting", "boolean", values=(True,)),)),
        RestOperationContract("mcp_auth_statuses", "IntegrationOperations.inspect_mcp", "GET", "/mcp/auth-statuses", "/api/v2/mcp/auth-statuses", query_examples=({"cwd": "/workspace", "verify": "false"},), response_fields=(_field("[]", "object"), _field("[].name", "string"), _field("[].authStatus", "string", values=("not-applicable", "bearer-token", "oauth-required", "oauth-authorized", "oauth-expired", "unavailable"))), api_version="v2"),
        RestOperationContract("account_quota", "IntegrationOperations.get_account_quota", "GET", "/oauth/usage", "/api/v1/oauth/usage", response_fields=(_field("kind", "string", values=("ok", "error")), _field("quota", "object", required=False), _field("quota.usages", "object", required=False), *tuple(field for window in ("limit5h", "limit7d", "monthTotal", "monthCode") for field in (_field(f"quota.usages.{window}", "object", required=False), _field(f"quota.usages.{window}.usedRatio", "number", required=False), _field(f"quota.usages.{window}.resetAt", "string", required=False))))),
        RestOperationContract(
            "side_question",
            "KimiServerClient.side_question",
            "POST",
            "/sessions/{session_id}:btw",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            schema_alias_note="The archive path exposes action requests but not the side-agent response; side-question creation validates the returned agent ID",
        ),
        RestOperationContract(
            "fork_session",
            "KimiServerClient.fork_session",
            "POST",
            "/sessions/{session_id}:fork",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            schema_alias_note="The archive path exposes action requests but not the fork session response; the lifecycle probe checks that response",
        ),
        RestOperationContract(
            "restore_session",
            "KimiServerClient.restore_session",
            "POST",
            "/sessions/{session_id}:restore",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            schema_alias_note="The archive path exposes action requests but not the restore session response; the lifecycle probe checks that response",
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
                        "swarm_mode": False,
                        "goal_objective": "Finish the task",
                        "goal_control": "pause",
                    },
                },
            ),
            response_fields=_PROFILE_FIELDS,
        ),
        RestOperationContract(
            "compact_session",
            "KimiServerClient.compact_session",
            "POST",
            "/sessions/{session_id}:compact",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({}, {"instruction": "Preserve the remaining work"}),
            request_fields=(_field("instruction", "string", required=False),),
            schema_alias_note=(
                "The OpenAPI session-action union is exposed under the "
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
                "The OpenAPI session-action union is exposed under the "
                "archive path; the runtime path remains :undo"
            ),
        ),
        RestOperationContract(
            "abort_session",
            "KimiServerClient.abort_session",
            "POST",
            "/sessions/{session_id}:abort",
            "/api/v1/sessions/{session_id}:archive",
            request_examples=({},),
            schema_alias_note=(
                "The OpenAPI session-action union is exposed under the "
                "archive path; the runtime path remains :abort"
            ),
        ),
        RestOperationContract(
            "goal",
            "KimiServerClient.get_goal",
            "GET",
            "/sessions/{session_id}/goal",
            "/api/v1/sessions/{session_id}/goal",
            response_fields=_GOAL_FIELDS,
        ),
        RestOperationContract(
            "edit_goal",
            "KimiServerClient.edit_goal",
            "PATCH",
            "/sessions/{session_id}/goal",
            "/api/v1/sessions/{session_id}/goal",
            request_examples=({"objective": "Revise the objective"},),
            response_fields=_GOAL_FIELDS,
        ),
        RestOperationContract(
            "submit_prompt",
            "KimiServerClient.submit_prompt",
            "POST",
            "/sessions/{session_id}/prompts",
            "/api/v1/sessions/{session_id}/prompts",
            request_examples=(
                {"agent_id": "side-agent", "content": [{"type": "text", "text": "Side question"}]},
                {"content": [{"type": "file", "file_id": "uploaded-fixture", "name": "fixture.txt", "media_type": "text/plain", "size": 7}]},
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
                                "kind": "file",
                                "file_id": "file-image",
                            },
                        },
                        {
                            "type": "video",
                            "source": {
                                "kind": "file",
                                "file_id": "file-video",
                            },
                        },
                    ]
                },
            ),
            response_fields=(
                _field("prompt_id", "string"),
                _field(
                    "status",
                    "string",
                    values=("running", "queued", "blocked"),
                ),
            ),
        ),
        RestOperationContract(
            "steer_prompts",
            "KimiServerClient.submit_prompt",
            "POST",
            "/sessions/{session_id}/prompts:steer",
            "/api/v1/sessions/{session_id}/prompts:steer",
            request_examples=({"prompt_ids": ["prompt-1"]},),
            response_fields=(_field("steered", "boolean"),),
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
                _field("tools.[].active", "boolean", required=False),
            ),
        ),
        RestOperationContract(
            "pending_interactions",
            "KimiServerClient._get_pending_interaction_ids",
            "GET",
            "/sessions/{session_id}/transcript",
            "/api/v1/sessions/{session_id}/transcript",
            query_examples=({"agent_id": "main", "page_size": 1},),
            response_fields=(
                _field("pending_interactions", "array"),
                _field("pending_interactions.[]", "string"),
                _field("agents", "array"),
                _field("agents.[].agentId", "string"),
            ),
        ),
        RestOperationContract(
            "respond_interaction",
            "KimiServerClient.resolve_approval/resolve_question/dismiss_question",
            "POST",
            "/sessions/{session_id}/interactions/{interaction_id}",
            "/api/v1/sessions/{session_id}/interactions/{interaction_id}",
            request_examples=(
                {"kind": "approval", "decision": "approved"},
                {"kind": "approval", "decision": "rejected"},
                {"kind": "approval", "decision": "cancelled"},
                {"kind": "question", "dismissed": True},
                {
                    "kind": "question",
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
            response_fields=(
                _field(
                    "outcome",
                    "string",
                    values=(
                        "approved",
                        "rejected",
                        "cancelled",
                        "answered",
                        "dismissed",
                    ),
                ),
            ),
        ),
        RestOperationContract(
            "list_prompts",
            "KimiServerClient.get_prompt_queue/abort_prompt/abort_session",
            "GET",
            "/sessions/{session_id}/prompts",
            "/api/v1/sessions/{session_id}/prompts",
            response_fields=(
                _field("active", "object"),
                _field("active.prompt_id", "string", required=False, required_if_parent_present=True),
                _field("active.status", "string", required=False, required_if_parent_present=True, values=("running", "queued", "blocked")),
                _field("active.content", "array", required=False, required_if_parent_present=True),
                _field("queued", "array"),
                _field("queued.[].prompt_id", "string"),
                _field("queued.[].status", "string", values=("running", "queued", "blocked")),
                _field("queued.[].content", "array"),
            ),
        ),
        RestOperationContract(
            "abort_prompt",
            "KimiServerClient.abort_prompt/abort_session",
            "POST",
            "/sessions/{session_id}/prompts/{prompt_id}:abort",
            "/api/v1/sessions/{session_id}/prompts/{tail}",
        ),
        RestOperationContract(
            "transcript",
            "KimiServerClient.get_snapshot",
            "GET",
            "/sessions/{session_id}/transcript",
            "/api/v1/sessions/{session_id}/transcript",
            query_examples=({"agent_id": "main", "page_size": 100},),
            response_fields=(
                _field("agent_id", "string"),
                _field("items", "array"),
                _field("items.[].kind", "string", values=("turn", "marker")),
                _field("items.[].turnId", "string"),
                _field("items.[].ordinal", "integer"),
                _field(
                    "items.[].state",
                    "string",
                    values=("queued", "running", "completed", "failed", "cancelled"),
                ),
                _field("items.[].triggerPromptId", "string", required=False),
                _field("items.[].prompt", "string", required=False),
                _field("items.[].steps", "array"),
                _field("items.[].steps.[].frames", "array"),
                _field("items.[].steps.[].frames.[].kind", "string"),
                _field("items.[].steps.[].frames.[].role", "string"),
                _field("items.[].steps.[].frames.[].text", "string"),
                _field("has_more", "boolean"),
                _field("prompts", "array"),
                _field("prompts.[].promptId", "string"),
                _field("prompts.[].status", "string"),
                _field("attachments", "array"),
                _field("attachments.[].attachmentId", "string"),
                _field("attachments.[].mediaType", "string"),
                _field("attachments.[].name", "string", required=False),
                _field("seq", "integer", required=False),
            ),
        ),
        RestOperationContract(
            "transcript_ops",
            "evaluate_kimi_semantic_contract/probe_kimi_compatibility",
            "GET",
            "/sessions/{session_id}/transcript/ops",
            "/api/v1/sessions/{session_id}/transcript/ops",
            query_examples=({"agent_id": "main", "since_seq": 0},),
            response_fields=(
                _field("agent_id", "string"),
                _field("batches", "array"),
                _field("batches.[].seq", "integer"),
                _field("batches.[].ops", "array"),
                _field("latest_seq", "integer"),
                _field("complete", "boolean"),
            ),
        ),
    )
}


_INTERACTION_FIELDS = (
    _field("type", "string", values=("interaction",)),
    _field("session_id", "string"),
    _field("agent_id", "string"),
    _field("interaction_id", "string"),
    _field("tool_call_id", "string", required=False),
    _field("status", "string", values=("pending", "ended")),
    _field("request", "object"),
)


KIMI_WEBSOCKET_MESSAGES: tuple[WebSocketMessageContract, ...] = (
    WebSocketMessageContract(
        "client_hello",
        "KimiServerClient._send_client_hello",
        (
            _field("type", "string", values=("client_hello",)),
            _field("id", "string"),
            _field("payload.client_id", "string"),
        ),
        (
            {
                "type": "client_hello",
                "id": "request-1",
                "payload": {"client_id": "kimi-bridge"},
            },
        ),
    ),
    WebSocketMessageContract(
        "client_hello_ack",
        "KimiServerClient._send_client_hello",
        (
            _field("type", "string", values=("ack",)),
            _field("id", "string"),
            _field("code", "integer", "number"),
            _field("msg", "string"),
        ),
    ),
    WebSocketMessageContract(
        "subscribe_v2",
        "KimiServerClient._send_subscribe_v2",
        (
            _field("type", "string", values=("subscribe_v2",)),
            _field("id", "string"),
            _field("payload.session_id", "string"),
            _field("payload.transcript", "object"),
            _field(
                "payload.transcript.{}",
                "string",
                required=False,
                values=("off", "turn", "block", "delta"),
            ),
            _field("payload.transcript_since", "object", required=False),
            _field("payload.transcript_since.{}", "integer", required=False),
        ),
        (
            {
                "type": "subscribe_v2",
                "id": "request-2",
                "payload": {
                    "session_id": "session-1",
                    "transcript": {"main": "delta"},
                    "transcript_since": {"main": 3},
                },
            },
        ),
    ),
    WebSocketMessageContract(
        "subscribe_v2_ack",
        "KimiServerClient._send_subscribe_v2",
        (
            _field("type", "string", values=("ack",)),
            _field("id", "string"),
            _field("code", "integer", "number"),
            _field("msg", "string"),
            _field("payload.accepted", "array"),
            _field("payload.accepted.[]", "string"),
            _field("payload.not_found", "array"),
            _field("payload.not_found.[]", "string"),
        ),
    ),
    WebSocketMessageContract(
        "server_hello",
        "KimiServerClient._expect_server_hello",
        (
            _field("type", "string", values=("server_hello",)),
            _field("payload.protocol_version", "integer", required=False),
        ),
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
        ({"type": "pong", "payload": {"nonce": "nonce-1"}},),
    ),
    WebSocketMessageContract(
        "error",
        "KimiServerClient._raise_ws_error",
        (
            _field("type", "string", values=("error",)),
            _field("payload.code", "integer", "number"),
            _field("payload.msg", "string"),
            _field("payload.fatal", "boolean", required=False),
        ),
    ),
    WebSocketMessageContract(
        "interaction",
        "KimiServerClient.subscribe_events",
        (
            *_INTERACTION_FIELDS,
            _field("kind", "string", values=("approval",)),
            _field(
                "outcome",
                "string",
                required=False,
                values=("approved", "rejected", "cancelled"),
            ),
            _field("request.tool_name", "string"),
            _field("request.action", "string"),
            _field("request.tool_input_display", "any", required=False),
        ),
        discriminator=("kind", "approval"),
    ),
    WebSocketMessageContract(
        "interaction",
        "KimiServerClient.subscribe_events",
        (
            *_INTERACTION_FIELDS,
            _field("kind", "string", values=("question",)),
            _field(
                "outcome",
                "string",
                required=False,
                values=("answered", "dismissed", "cancelled"),
            ),
            _field("request.questions", "array"),
            _field("request.questions.[].id", "string"),
            _field("request.questions.[].question", "string"),
            _field("request.questions.[].header", "string", required=False),
            _field("request.questions.[].body", "string", required=False),
            _field("request.questions.[].options", "array"),
            _field("request.questions.[].options.[].id", "string"),
            _field("request.questions.[].options.[].label", "string"),
            _field(
                "request.questions.[].options.[].description", "string", required=False
            ),
            _field("request.questions.[].multi_select", "boolean", required=False),
            _field("request.questions.[].allow_other", "boolean", required=False),
            _field("request.questions.[].other_label", "string", required=False),
        ),
        discriminator=("kind", "question"),
    ),
    WebSocketMessageContract(
        "session_event",
        "KimiServerClient.subscribe_events global push envelope",
        (
            _field("type", "string"),
            _field("session_id", "string", required=False),
            _field("payload", "object"),
            _field("payload.type", "string"),
        ),
    ),
    WebSocketMessageContract(
        "agent",
        "KimiServerClient.subscribe_events/get_session_status",
        (
            _field("type", "string", values=("agent",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("status", "string", values=("idle", "running")),
            _field("model", "string", required=False),
            _field("thinking", "string", required=False),
            _field(
                "permission_mode",
                "string",
                required=False,
                values=("manual", "auto", "yolo"),
            ),
            _field("plan_mode", "boolean"),
        ),
    ),
    WebSocketMessageContract(
        "usage",
        "KimiServerClient.subscribe_events/get_session_usage",
        (
            _field("type", "string", values=("usage",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("context_tokens", "integer"),
            _field("max_context_tokens", "integer", required=False),
        ),
    ),
    WebSocketMessageContract(
        "goal",
        "KimiServerClient.subscribe_events agent feed",
        (
            _field("type", "string", values=("goal",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("goal", "object", required=False),
            _field("goal.objective", "string", required=False),
            _field("goal.status", "string", required=False, values=("active", "paused", "blocked", "complete")),
        ),
    ),
    WebSocketMessageContract(
        "todo",
        "KimiServerClient.subscribe_events agent feed",
        (
            _field("type", "string", values=("todo",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("items", "array"),
            _field("items.[].title", "string"),
            _field("items.[].status", "string", values=("pending", "in_progress", "done")),
        ),
    ),
    WebSocketMessageContract(
        "task_list",
        "KimiServerClient.list_tasks",
        (
            _field("type", "string", values=("task.list",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("tasks", "array"),
            *_TASK_LIST_FRAME_FIELDS,
        ),
    ),
    WebSocketMessageContract(
        "task",
        "KimiServerClient.subscribe_events task feed",
        (
            _field("type", "string", values=("task",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            *_TASK_FRAME_FIELDS,
        ),
    ),
    WebSocketMessageContract(
        "task_output",
        "KimiServerClient.get_task",
        (
            _field("type", "string", values=("task.output",)),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("task_id", "string"),
            _field("offset", "integer"),
            _field("text", "string"),
        ),
    ),
    WebSocketMessageContract(
        "task_watch",
        "KimiServerClient.get_task",
        (
            _field("type", "string", values=("task.watch",)),
            _field("request_id", "string"),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("task_id", "string"),
            _field("tail_bytes", "integer", required=False),
        ),
        (
            {
                "type": "task.watch",
                "request_id": "request-3",
                "session_id": "session-1",
                "agent_id": "main",
                "task_id": "bash-1",
                "tail_bytes": 8192,
            },
        ),
    ),
    WebSocketMessageContract(
        "task_stop",
        "KimiServerClient.cancel_task",
        (
            _field("type", "string", values=("task.stop",)),
            _field("request_id", "string"),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("task_id", "string"),
        ),
        (
            {
                "type": "task.stop",
                "request_id": "request-4",
                "session_id": "session-1",
                "agent_id": "main",
                "task_id": "bash-1",
            },
        ),
    ),
    WebSocketMessageContract(
        "task_background",
        "KimiServerClient.background_task",
        (
            _field("type", "string", values=("task.background",)),
            _field("request_id", "string"),
            _field("session_id", "string"),
            _field("agent_id", "string"),
            _field("task_id", "string"),
        ),
        (
            {
                "type": "task.background",
                "request_id": "request-background",
                "session_id": "session-1",
                "agent_id": "main",
                "task_id": "bash-1",
            },
        ),
    ),
    WebSocketMessageContract(
        "task_watch_ack",
        "KimiServerClient.get_task response",
        (
            _field("type", "string", values=("response",)),
            _field("request_id", "string"),
            _field("code", "integer", "number"),
            _field("msg", "string", required=False),
            _field("task", "object", required=False),
            *_TASK_RESPONSE_FIELDS,
        ),
    ),
    WebSocketMessageContract(
        "task_stop_ack",
        "KimiServerClient.cancel_task response",
        (
            _field("type", "string", values=("response",)),
            _field("request_id", "string"),
            _field("code", "integer", "number"),
            _field("task", "object", required=False),
            *_TASK_RESPONSE_FIELDS,
        ),
    ),
)


KIMI_TRANSCRIPT_OPS: tuple[TranscriptOpContract, ...] = (
    TranscriptOpContract(
        "reset",
        "TranscriptProjection.apply_reset",
        (
            _field("agentId", "string"),
            _field("snapshot", "object"),
            _field("snapshot.items", "array"),
        ),
    ),
    TranscriptOpContract(
        "turn.upsert",
        "TranscriptProjection._apply_turn_upsert",
        (
            _field("turn.kind", "string", values=("turn",)),
            _field("turn.turnId", "string"),
            _field("turn.ordinal", "integer"),
            _field(
                "turn.state",
                "string",
                values=("queued", "running", "completed", "failed", "cancelled"),
            ),
            _field("turn.prompt", "string", required=False),
        ),
    ),
    TranscriptOpContract(
        "step.upsert",
        "TranscriptProjection._apply_step_upsert",
        (
            _field("turnId", "string"),
            _field("step.stepId", "string"),
            _field("step.ordinal", "integer"),
            _field(
                "step.state",
                "string",
                values=("running", "completed", "interrupted", "failed"),
            ),
        ),
    ),
    TranscriptOpContract(
        "frame.upsert",
        "TranscriptProjection._apply_frame_upsert",
        (
            _field("turnId", "string"),
            _field("stepId", "string"),
            _field("frame.frameId", "string"),
            _field(
                "frame.kind", "string", values=("text", "thinking", "tool", "notice")
            ),
            _field("frame.text", "string", required=False),
            _field("frame.role", "string", required=False),
        ),
    ),
    TranscriptOpContract(
        "append",
        "TranscriptProjection._apply_append",
        (
            _field("target.type", "string", values=("frame",)),
            _field("target.turnId", "string"),
            _field("target.stepId", "string"),
            _field("target.frameId", "string"),
            _field("offset", "integer"),
            _field("text", "string"),
        ),
    ),
    TranscriptOpContract(
        "marker.upsert",
        "TranscriptProjection._apply_marker_upsert",
        (
            _field("item.kind", "string", values=("marker",)),
            _field("item.marker", "string"),
            _field("item.payload", "object", required=False),
        ),
    ),
    TranscriptOpContract(
        "prompt.upsert",
        "TranscriptProjection._apply_prompt_upsert",
        (
            _field("prompt.promptId", "string"),
            _field(
                "prompt.status",
                "string",
                values=(
                    "running",
                    "queued",
                    "blocked",
                    "completed",
                    "failed",
                    "aborted",
                ),
            ),
        ),
    ),
    TranscriptOpContract(
        "items.remove",
        "TranscriptProjection state pruning",
        (_field("ids", "array"),),
    ),
    TranscriptOpContract(
        "attachment.upsert",
        "TranscriptProjection snapshot attachments",
        (
            _field("attachment.attachmentId", "string"),
            _field("attachment.mediaType", "string"),
        ),
    ),
    TranscriptOpContract(
        "interaction.upsert",
        "Flat interaction frames provide session-wide delivery",
        (_field("interaction", "object"),),
    ),
)


KIMI_SESSION_EVENTS: tuple[SessionEventContract, ...] = (
    SessionEventContract(
        "turn.started",
        "TranscriptProjection/ChatRouter.dispatch_event",
        (_field("turnId", "integer", "number"),),
    ),
    SessionEventContract(
        "turn.step.started",
        "TranscriptProjection/ChatRouter.dispatch_event",
        (_field("step", "integer", "number"),),
    ),
    SessionEventContract(
        "assistant.delta",
        "TranscriptProjection/ChatRouter.dispatch_event",
        (_field("delta", "string"),),
    ),
    SessionEventContract(
        "thinking.delta",
        "TranscriptProjection/ChatRouter.dispatch_event",
        (_field("delta", "string"),),
    ),
    SessionEventContract(
        "turn.ended",
        "TranscriptProjection/ChatRouter.dispatch_event/session_notice_from_event",
        (
            _field("turnId", "integer", "number"),
            _field(
                "reason",
                "string",
                values=("completed", "cancelled", "failed"),
            ),
            _field("error", "object", required=False),
        ),
    ),
    SessionEventContract(
        "error",
        "TranscriptProjection/session_notice_from_event",
        (
            _field("code", "string"),
            _field("message", "string"),
            _field("retryable", "boolean"),
        ),
    ),
    SessionEventContract(
        "warning",
        "TranscriptProjection/session_notice_from_event",
        (
            _field("message", "string"),
            _field("code", "string", required=False),
        ),
    ),
    SessionEventContract(
        "prompt.completed",
        "TranscriptProjection/ChatRouter.dispatch_event",
        (_field("promptId", "string"),),
    ),
    SessionEventContract(
        "compaction.started",
        "TranscriptProjection/ChatRouter._dispatch_compaction_event",
        (_field("trigger", "string", values=("manual", "auto")),),
    ),
    SessionEventContract(
        "compaction.completed",
        "TranscriptProjection/ChatRouter._dispatch_compaction_event",
        (
            _field("result", "object"),
            _field("result.compactedCount", "integer", "number"),
            _field("result.tokensBefore", "integer", "number"),
            _field("result.tokensAfter", "integer", "number"),
        ),
    ),
    SessionEventContract(
        "compaction.blocked",
        "TranscriptProjection/ChatRouter._dispatch_compaction_event",
    ),
    SessionEventContract(
        "compaction.cancelled",
        "TranscriptProjection/ChatRouter._dispatch_compaction_event",
    ),
)


KIMI_LIFECYCLE_INVARIANTS: tuple[tuple[str, str], ...] = (
    ("startup.foreground", "KimiServerSupervisor._run_child"),
    ("startup.bearer_token", "parse_server_startup_line"),
    ("server.intentional_restart", "KimiServerSupervisor.restart"),
    ("rest.bearer_auth", "KimiServerClient._request"),
    ("session.create", "KimiServerClient.create_session"),
    ("websocket.bearer_auth", "KimiServerClient.subscribe_events"),
    ("websocket.subscribe_v2_resumes", "KimiServerClient._send_subscribe_v2"),
    ("websocket.agent_burst_before_ack", "KimiServerClient._wait_for_ack"),
    ("websocket.transcript_seq_resume", "KimiServerClient.subscribe_events"),
)


def kimi_semantic_contract() -> dict[str, Any]:
    """Return the stable, machine-readable contract consumed by automation."""

    def field_payload(item: SchemaFieldContract) -> dict[str, Any]:
        payload = {
            "path": list(item.path),
            "types": list(item.types),
            "required": item.required,
            "values": list(item.values),
        }
        if item.format is not None:
            payload["format"] = item.format
        if item.required_if_parent_present:
            payload["required_if_parent_present"] = True
        return payload

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
                "request_media_type": item.request_media_type,
                "response_media_type": item.response_media_type,
                "api_version": item.api_version,
                "request_examples": list(item.request_examples),
                "request_fields": [
                    field_payload(field) for field in item.request_fields
                ],
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
                    **(
                        {"discriminator": dict([item.discriminator])}
                        if item.discriminator is not None
                        else {}
                    ),
                }
                for item in KIMI_WEBSOCKET_MESSAGES
            ],
            "transcript": {
                "frames": ["transcript.reset", "transcript.ops"],
                "ops": [
                    {
                        "op": item.op,
                        "source": item.source,
                        "fields": [field_payload(field) for field in item.fields],
                    }
                    for item in KIMI_TRANSCRIPT_OPS
                ],
            },
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
