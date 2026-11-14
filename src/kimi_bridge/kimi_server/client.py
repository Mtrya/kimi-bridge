"""Typed REST and WebSocket client for the managed Kimi server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from ..compatibility import (
    VersionSupport,
    classify_kimi_code_version,
    normalize_kimi_code_version,
    unknown_version_warning,
)
from ..interactions import (
    ApprovalDecision,
    QuestionAnswer,
)
from .contract import (
    KIMI_ASYNCAPI_PATH,
    KIMI_OPENAPI_PATH,
    KIMI_REST_OPERATIONS,
    KIMI_WEBSOCKET_PATH,
)
from .workspace import WorkspaceOperations
from .file_history import FileHistoryOperations
from .integrations import IntegrationOperations
from .activity import ActivityProjection
from .conversation import ConversationProjection
from .prompts import deliver_prompt
from .supervisor import KimiServerSupervisor
from .turns import snapshot_from_turns, turn_opens_with_user_input, turn_prompt_id
from .types import (
    ActivityUpdate,
    SessionActivity,
    BackgroundOutcome,
    GoalControl,
    GoalInfo,
    InteractionResolution,
    InteractionSnapshot,
    InteractionUpdate,
    KimiServerAPIError,
    KimiServerOperationError,
    KimiServerProtocolError,
    KimiServerStartupError,
    KimiServerTransportError,
    ModelInfo,
    PermissionMode,
    PromptContent,
    PromptDelivery,
    PromptMedia,
    PromptQueue,
    PromptOutcome,
    PromptSubmission,
    SecondaryModelConfig,
    ServerConnection,
    SessionArchiveUncertain,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    TaskStatus,
    ToolInfo,
)
from .task_output import TaskOutputTail
from .wire import (
    _goal_info_from_wire,
    _interaction_update_from_wire,
    _model_info_from_wire,
    _question_answer_to_wire,
    _queued_prompt_from_wire,
    _secondary_model_config_from_wire,
    _session_profile_from_wire,
    _session_status_from_wire,
    _session_usage_from_wire,
    _skill_info_from_wire,
    _task_info_from_wire,
    _tool_info_from_wire,
)


LOGGER = logging.getLogger(__name__)
_MAIN_AGENT_ID = "main"
_MAX_CONSECUTIVE_RESPONSE_MISMATCHES = 3
_TASK_OUTPUT_GRACE_SECONDS = 1.0
_CONVERSATION_FRAME_TYPES = frozenset(
    {
        "turns",
        "turns.removed",
        "turn.started",
        "turn.updated",
        "turn.ended",
        "item.started",
        "item.updated",
        "item.ended",
        "item.delta",
        "item.removed",
        "notice",
    }
)


class _WebSocketResponseMismatch(KimiServerProtocolError):
    """A reconnect handshake received a frame for a different request."""


class KimiServerClient(WorkspaceOperations, FileHistoryOperations, IntegrationOperations):
    """Thin async REST and WebSocket client for a local kimi server."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        *,
        supervisor: KimiServerSupervisor | None = None,
        timeout: float = 30.0,
        http_client: Any | None = None,
        ws_connect: Callable[..., Any] = websockets.connect,
        reconnect_initial_backoff: float = 0.25,
        reconnect_max_backoff: float = 5.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if supervisor is None:
            if base_url is None or token is None:
                raise TypeError("base_url and token are required without a supervisor")
            parsed = urlsplit(base_url)
            if parsed.hostname is None or parsed.port is None:
                raise ValueError("base_url must include a host and port")
            self._fixed_connection = ServerConnection(
                base_url=base_url.rstrip("/"),
                port=parsed.port,
                generation=0,
                token=token,
            )
        else:
            if base_url is not None or token is not None:
                raise TypeError("use either supervisor or base_url/token, not both")
            self._fixed_connection = None

        self._supervisor = supervisor
        self._http = http_client or httpx.AsyncClient(timeout=timeout)
        self._timeout = timeout
        self._owns_http = http_client is None
        self._ws_connect = ws_connect
        self._sleep = sleep
        self._reconnect_initial_backoff = reconnect_initial_backoff
        self._reconnect_max_backoff = reconnect_max_backoff
        self._subscription_locks: dict[tuple[str, str], asyncio.Lock] = {}
        self._secondary_model_lock = asyncio.Lock()
        self._subscription_ready: dict[str, asyncio.Event] = {}
        self._agent_frames: dict[str, dict[str, Any]] = {}
        self._usage_frames: dict[str, dict[str, Any]] = {}
        self._active_ws: dict[tuple[str, str], Any] = {}
        self._activities: dict[str, ActivityProjection] = {}
        self._task_owners: dict[tuple[str, str], str] = {}
        self._manual_compactions: set[str] = set()
        self._closed = False

    async def __aenter__(self) -> KimiServerClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        await asyncio.gather(*(ws.close() for ws in tuple(self._active_ws.values())))
        if self._owns_http:
            await self._http.aclose()

    async def get_openapi_document(self) -> dict[str, Any]:
        """Fetch the complete live REST description without its API envelope."""

        return await self._request_document(KIMI_OPENAPI_PATH)

    async def get_asyncapi_document(self) -> dict[str, Any]:
        """Fetch the complete live WebSocket description."""

        return await self._request_document(KIMI_ASYNCAPI_PATH)

    async def meta(self) -> dict[str, Any]:
        return await self._request_operation("meta")

    async def get_config(self) -> dict[str, Any]:
        """Return the server's resolved, secret-redacted configuration."""

        return await self._request_operation("config")

    async def get_default_model(self) -> str:
        """Return the server's configured default model."""

        config = await self.get_config()
        model = config.get("default_model")
        if not isinstance(model, str) or not model:
            raise KimiServerProtocolError(
                "kimi server configuration has no default_model"
            )
        return model

    async def get_secondary_model(self) -> SecondaryModelConfig:
        """Return the global model selection used for newly spawned subagents."""

        config = await self.get_config()
        return _secondary_model_config_from_wire(config.get("secondary_model"))

    async def set_secondary_model(self, model: str) -> SecondaryModelConfig:
        """Persist the global default model for newly spawned subagents."""

        if not model:
            raise ValueError("secondary model must be non-empty")
        async with self._secondary_model_lock:
            current = await self.get_secondary_model()
            secondary_patch: dict[str, Any] = {"default_model": model}
            if current.models is not None and model not in dict(current.models):
                secondary_patch["models"] = {model: ""}
            data = await self._request_operation(
                "update_config",
                json_body={"secondary_model": secondary_patch},
            )
            updated = _secondary_model_config_from_wire(data.get("secondary_model"))
            if updated.default_model != model:
                raise KimiServerProtocolError(
                    f"kimi server did not persist secondary model {model!r}"
                )
            return updated

    async def set_secondary_model_entry(
        self, model: str, description: str
    ) -> SecondaryModelConfig:
        """Add or describe a subagent pool entry without changing its default."""

        if not model:
            raise ValueError("secondary model must be non-empty")
        if model == "primary":
            raise KimiServerOperationError(
                "primary is reserved and cannot be a secondary model pool alias"
            )
        async with self._secondary_model_lock:
            current = await self.get_secondary_model()
            models = {model: description}
            if (
                current.models is None
                and current.default_model is not None
                and current.default_model != model
            ):
                models[current.default_model] = ""
            patch: dict[str, Any] = {"models": models}
            if current.default_model is None:
                patch["default_model"] = model
            data = await self._request_operation(
                "update_config", json_body={"secondary_model": patch}
            )
            updated = _secondary_model_config_from_wire(data.get("secondary_model"))
            if (
                updated.default_model != (current.default_model or model)
                or dict(updated.models or ()).get(model) != description
            ):
                raise KimiServerProtocolError(
                    f"kimi server did not persist secondary model pool entry {model!r}"
                )
            return updated

    async def get_server_version(self) -> str:
        """Return the managed server's advertised version."""

        metadata = await self.meta()
        server_version = metadata.get("server_version")
        if not isinstance(server_version, str) or not server_version:
            raise KimiServerProtocolError(
                "kimi server metadata has no server_version"
            )
        return server_version

    async def reload_session(self, session_id: str) -> None:
        """Reload Kimi configuration and rebuild an idle session in place."""

        await self._request_operation(
            "reload_session",
            path_parameters={"session_id": session_id},
            json_body={},
        )

    async def restart_server(self) -> ServerConnection:
        """Restart the supervised Kimi server and wait for its replacement."""

        if self._supervisor is None:
            raise KimiServerOperationError(
                "server restart requires a managed kimi server"
            )
        connection = await self._supervisor.restart()
        self._agent_frames.clear()
        self._usage_frames.clear()
        return connection

    async def check_server_version(
        self, *, executable_version: str | None = None
    ) -> str:
        reported_version = await self.get_server_version()
        try:
            server_version = normalize_kimi_code_version(reported_version)
        except ValueError as exc:
            raise KimiServerStartupError(
                "kimi server reported a malformed version"
            ) from exc
        if executable_version is None and self._supervisor is not None:
            executable_version = self._supervisor.executable_identity.version
        if executable_version is not None:
            normalized_executable_version = normalize_kimi_code_version(
                executable_version
            )
            if server_version != normalized_executable_version:
                raise KimiServerStartupError(
                    "kimi-code executable/server version mismatch: "
                    f"executable {normalized_executable_version}, "
                    f"server {server_version}"
                )

        support = classify_kimi_code_version(server_version)
        if support is VersionSupport.SUPPORTED:
            LOGGER.info("kimi server version: %s (supported)", server_version)
        elif self._supervisor is None:
            LOGGER.warning("%s", unknown_version_warning(server_version))
        else:
            LOGGER.info(
                "kimi server version: %s (matches untested executable)",
                server_version,
            )
        return server_version

    async def list_models(self) -> list[ModelInfo]:
        """Return the exact configured model aliases and capabilities."""

        data = await self._request_operation("models")
        return [_model_info_from_wire(item) for item in data["items"]]

    async def get_session_model(self, session_id: str) -> ModelInfo:
        """Resolve the exact model currently bound to a session."""

        status, models = await asyncio.gather(
            self.get_session_status(session_id),
            self.list_models(),
        )
        if status.model is None:
            raise KimiServerProtocolError(
                f"kimi session {session_id} has no configured model"
            )
        for model in models:
            if model.alias == status.model:
                return model
        raise KimiServerProtocolError(
            f"kimi session {session_id} uses unavailable model {status.model!r}"
        )

    async def create_session(
        self,
        workspace: str,
        *,
        title: str | None = None,
        environment_id: str | None = None,
        environment_cwd: str | None = None,
        **profile: Any,
    ) -> str:
        payload: dict[str, Any] = {
            "metadata": {"cwd": str(Path(workspace).expanduser().resolve())}
        }
        if title is not None:
            payload["title"] = title
        if environment_id is not None:
            payload["environment_id"] = environment_id
        if environment_cwd is not None:
            payload["environment_cwd"] = environment_cwd
        data = await self._request_operation(
            "create_session", json_body=payload
        )
        session_id = str(data["id"])
        if profile:
            try:
                await self.update_profile(session_id, **profile)
            except KimiServerOperationError as exc:
                raise KimiServerOperationError(
                    f"Session {session_id} was created, but its settings could not be applied: {exc}"
                ) from exc
        return session_id

    async def fork_session(self, session_id: str) -> str:
        data = await self._request_operation(
            "fork_session", path_parameters={"session_id": session_id}, json_body={}
        )
        return str(data["id"])

    async def restore_session(self, session_id: str) -> str:
        data = await self._request_operation(
            "restore_session", path_parameters={"session_id": session_id}, json_body={}
        )
        return str(data["id"])

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request_operation(
            "get_session", path_parameters={"session_id": session_id}
        )

    async def find_session(self, session_id: str) -> dict[str, Any] | None:
        """Look up a session without exposing the server's missing-ID code."""

        try:
            return await self.get_session(session_id)
        except KimiServerAPIError as exc:
            if exc.code == 40401:
                return None
            raise

    async def get_session_profile(self, session_id: str) -> SessionProfile:
        data = await self._request_operation(
            "get_profile", path_parameters={"session_id": session_id}
        )
        return _session_profile_from_wire(data)

    async def archive_session(self, session_id: str) -> None:
        """Archive one session and require confirmation before detaching it."""

        try:
            data = await self._request_operation(
                "archive_session",
                path_parameters={"session_id": session_id},
                json_body={},
            )
        except KimiServerTransportError as archive_error:
            # An archive can commit before its acknowledgement is lost. Read
            # stored metadata before any caller reconnects the session runtime.
            try:
                archived = await self.is_session_archived(session_id)
            except (KimiServerTransportError, KimiServerAPIError):
                raise SessionArchiveUncertain(
                    "Archive could not be confirmed. Retry /archive before continuing."
                ) from archive_error
            if archived:
                return
            raise
        if data.get("archived") is not True:
            raise KimiServerOperationError(
                f"kimi server did not confirm archiving session {session_id!r}"
            )

    async def is_session_archived(self, session_id: str) -> bool:
        """Read stored archive state without materializing the session runtime."""

        session = await self.get_session(session_id)
        return session.get("archived") is True

    async def get_session_status(self, session_id: str) -> SessionStatus:
        """Combine stored work state with effective settings from the live feed."""

        data = await self._request_operation(
            "get_session", path_parameters={"session_id": session_id}
        )
        return _session_status_from_wire(
            data,
            agent=self._agent_frames.get(session_id),
            usage=self._usage_frames.get(session_id),
        )

    async def get_session_usage(self, session_id: str) -> SessionUsage:
        """Read cumulative native counters, with live context-window values."""

        data = await self.get_session(session_id)
        return _session_usage_from_wire(
            data["usage"], live=self._usage_frames.get(session_id)
        )

    async def inspect_session(self, session_id: str) -> SessionStatus:
        """Read effective settings before a long-lived subscription exists."""

        data = await self.get_session(session_id)
        async with self._task_socket() as ws:
            async with asyncio.timeout(self._timeout):
                _, frames = await self._subscribe(
                    ws, session_id, [_MAIN_AGENT_ID]
                )
        agent = next(
            (frame for frame in frames
             if frame.get("type") == "agent"
             and frame.get("session_id") == session_id
             and frame.get("agent_id") == _MAIN_AGENT_ID),
            None,
        )
        if agent is None:
            raise KimiServerProtocolError("session inspection delivered no main-agent state")
        return _session_status_from_wire(data, agent=agent)

    async def compact_session(
        self,
        session_id: str,
        *,
        instruction: str | None = None,
    ) -> None:
        """Start manual context compaction with optional summary guidance."""

        self._manual_compactions.add(session_id)
        try:
            await self._request_operation(
                "compact_session",
                path_parameters={"session_id": session_id},
                json_body=(
                    {"instruction": instruction}
                    if instruction and instruction.strip()
                    else {}
                ),
            )
        except KimiServerAPIError:
            self._manual_compactions.discard(session_id)
            raise

    async def undo_session(self, session_id: str, *, count: int = 1) -> None:
        """Undo the requested number of public session history steps."""

        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("undo count must be a positive integer")
        remaining = count
        async for page in self._turn_pages(session_id, _MAIN_AGENT_ID, page_size=200):
            for turn in reversed(page["turns"]):
                if not turn_opens_with_user_input(turn):
                    continue
                remaining -= 1
                if remaining:
                    continue
                turn_id = turn["turn_id"]
                data = await self._request_operation(
                    "undo_session",
                    path_parameters={"session_id": session_id},
                    json_body={"turn_id": turn_id},
                )
                removed = data.get("removed_turn_ids") if isinstance(data, dict) else None
                if (
                    not isinstance(removed, list)
                    or any(not isinstance(value, str) for value in removed)
                    or turn_id not in removed
                ):
                    raise KimiServerProtocolError("undo response omitted its target turn")
                return
        raise KimiServerOperationError("No user-opened turn is available to undo.")

    async def get_goal(self, session_id: str) -> GoalInfo | None:
        """Return the session's current goal, if any."""

        data = await self._request_operation(
            "goal", path_parameters={"session_id": session_id}
        )
        if data is None:
            return None
        return _goal_info_from_wire(data)

    async def edit_goal(self, session_id: str, objective: str) -> GoalInfo:
        data = await self._request_operation(
            "edit_goal", path_parameters={"session_id": session_id},
            json_body={"objective": objective},
        )
        return _goal_info_from_wire(data)

    async def get_prompt_queue(self, session_id: str) -> PromptQueue:
        data = await self._request_operation(
            "list_prompts", path_parameters={"session_id": session_id}
        )
        return PromptQueue(
            active=(
                _queued_prompt_from_wire(data["active"])
                if data["active"] is not None else None
            ),
            waiting=tuple(_queued_prompt_from_wire(item) for item in data["queued"]),
        )

    async def cancel_prompt(self, session_id: str, prompt_id: str) -> bool:
        return await self._abort_prompt_by_id(session_id, prompt_id)

    async def steer_prompt(self, session_id: str, prompt_id: str) -> None:
        await self._request_operation(
            "steer_prompts", path_parameters={"session_id": session_id},
            json_body={"prompt_ids": [prompt_id]},
        )

    async def list_sessions(
        self,
        *,
        busy: bool | None = False,
        include_archive: bool = False,
        exclude_empty: bool = False,
        archived_only: bool = False,
        page_size: int | None = None,
        before_id: str | None = None,
        after_id: str | None = None,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "include_archive": include_archive,
            "exclude_empty": exclude_empty,
            "archived_only": archived_only,
        }
        if busy is not None:
            params["busy"] = busy
        if page_size is not None:
            params["page_size"] = page_size
        if before_id is not None:
            params["before_id"] = before_id
        if after_id is not None:
            params["after_id"] = after_id
        if workspace_id is not None:
            params["workspace_id"] = workspace_id
        data = await self._request_operation("list_sessions", params=params)
        return list(data["items"])

    async def list_all_sessions(
        self,
        *,
        busy: bool | None = False,
        include_archive: bool = False,
        exclude_empty: bool = False,
        page_size: int = 50,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Page through the full session set, newest first.

        The server returns sessions newest-first and treats ``after_id``
        as "newer than the cursor", so full pagination must advance with
        ``before_id`` (older than the cursor).
        """
        sessions: list[dict[str, Any]] = []
        before_id: str | None = None
        while True:
            page = await self.list_sessions(
                busy=busy,
                include_archive=include_archive,
                exclude_empty=exclude_empty,
                page_size=page_size,
                before_id=before_id,
                workspace_id=workspace_id,
            )
            if not page:
                break
            sessions.extend(page)
            last_id = str(page[-1]["id"])
            if len(page) < page_size or last_id == before_id:
                break
            before_id = last_id
        return sessions

    async def submit_prompt(
        self,
        session_id: str,
        content: str | PromptContent,
        *,
        delivery: PromptDelivery = PromptDelivery.ENQUEUE,
        model: str | None = None,
        thinking: str | None = None,
        permission_mode: PermissionMode | None = None,
        plan_mode: bool | None = None,
        agent_id: str | None = None,
    ) -> PromptSubmission:
        if not isinstance(delivery, PromptDelivery):
            raise ValueError("delivery must be a PromptDelivery")
        content_items = await self._prompt_content_to_wire(content)
        payload: dict[str, Any] = {"content": content_items}
        if model is not None:
            payload["model"] = model
        if thinking is not None:
            payload["thinking"] = thinking
        if permission_mode is not None:
            payload["permission_mode"] = permission_mode
        if plan_mode is not None:
            payload["plan_mode"] = plan_mode
        if agent_id is not None:
            payload["agent_id"] = agent_id
        connection = await self._connection_info()

        async def request(operation: str, body: dict[str, Any]) -> Any:
            return await self._request_operation(
                operation,
                path_parameters={"session_id": session_id},
                json_body=body,
                connection=connection,
            )

        return await deliver_prompt(request, payload, delivery)

    async def side_question(
        self, session_id: str, question: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Ask the native read-only side agent once, observing its own conversation."""

        data = await self._request_operation(
            "side_question", path_parameters={"session_id": session_id}, json_body={}
        )
        agent_id = data["agent_id"]
        if not isinstance(agent_id, str) or not agent_id:
            raise KimiServerProtocolError("side-question creation returned no agent")
        stream = self.subscribe_events(session_id, agent_id=agent_id)
        prompt_id: str | None = None
        try:
            async for event in stream:
                if isinstance(event, InteractionSnapshot):
                    if prompt_id is None:
                        receipt = await self.submit_prompt(
                            session_id, question, agent_id=agent_id
                        )
                        prompt_id = receipt.prompt_id
                        if receipt.outcome in {PromptOutcome.BLOCKED, PromptOutcome.FAILED}:
                            raise KimiServerOperationError(
                                "Side question did not start execution"
                            )
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("payload", {}).get("type") == "resync_required":
                    terminal = await self._terminal_turn_for_prompt(
                        session_id, agent_id, prompt_id
                    )
                    if terminal is not None:
                        reason = {
                            "completed": "completed",
                            "interrupted": "cancelled",
                            "failed": "failed",
                        }[terminal["outcome"]]
                        yield {
                            "session_id": session_id,
                            "payload": {
                                "type": "turn.ended",
                                "turnId": terminal["ordinal"],
                                "reason": reason,
                            },
                            "snapshot": event["snapshot"],
                        }
                        return
                ended = event.get("payload", {}).get("type") == "turn.ended"
                if ended:
                    event = {
                        **event,
                        "snapshot": await self.get_snapshot(
                            session_id, agent_id=agent_id
                        ),
                    }
                yield event
                if ended:
                    return
        finally:
            await stream.aclose()
            if prompt_id is not None:
                try:
                    await self._abort_prompt_by_id(session_id, prompt_id)
                except KimiServerOperationError:
                    LOGGER.warning(
                        "could not cancel the native side question", exc_info=True
                    )

    async def _prompt_content_to_wire(
        self, content: str | PromptContent
    ) -> list[dict[str, Any]]:
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
        items: list[dict[str, Any]] = []
        if content.text:
            items.append({"type": "text", "text": content.text})
        for media in content.media:
            file_id = await self._upload_prompt_media(media)
            items.append(
                {"type": "file", "file_id": file_id, "name": media.name, "media_type": media.media_type, "size": len(media.data)}
                if media.kind == "file"
                else {"type": media.kind, "source": {"kind": "file", "file_id": file_id}}
            )
        return items

    async def _upload_prompt_media(self, media: PromptMedia) -> str:
        data = await self._request_operation(
            "upload_file",
            form_body={"name": media.name},
            files={
                "file": (
                    media.name,
                    media.data,
                    media.media_type,
                )
            },
        )
        file_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(file_id, str) or not file_id:
            raise KimiServerProtocolError(
                "kimi server file upload returned no file id"
            )
        return file_id

    async def update_profile(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        permission_mode: PermissionMode | None = None,
        plan_mode: bool | None = None,
        swarm_mode: bool | None = None,
        goal_objective: str | None = None,
        goal_control: GoalControl | None = None,
    ) -> SessionProfile:
        payload: dict[str, Any] = {}
        if title is not None:
            payload["title"] = title
        agent_config: dict[str, Any] = {}
        if model is not None:
            agent_config["model"] = model
        if thinking is not None:
            agent_config["thinking"] = thinking
        if permission_mode is not None:
            agent_config["permission_mode"] = permission_mode
        if plan_mode is not None:
            agent_config["plan_mode"] = plan_mode
        if swarm_mode is not None:
            agent_config["swarm_mode"] = swarm_mode
        if goal_objective is not None:
            agent_config["goal_objective"] = goal_objective
        if goal_control is not None:
            agent_config["goal_control"] = goal_control
        if agent_config:
            payload["agent_config"] = agent_config
        if not payload:
            raise ValueError("profile update must contain at least one field")
        data = await self._request_operation(
            "update_profile",
            path_parameters={"session_id": session_id},
            json_body=payload,
        )
        return _session_profile_from_wire(data)

    async def list_tasks(
        self, session_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskInfo]:
        activity = await self.inspect_activity(session_id)
        return [task for task in activity.tasks if task.status == "running" and (status is None or task.status == status)]

    async def inspect_activity(self, session_id: str) -> SessionActivity:
        projection = self._activities.get(session_id)
        if projection is None:
            projection = ActivityProjection(session_id)
            async with self._task_socket() as ws:
                async with asyncio.timeout(self._timeout):
                    _, frames = await self._subscribe(
                        ws, session_id, [_MAIN_AGENT_ID]
                    )
                    frames = await self._subscribe_activity_agents(
                        ws, session_id, frames, {_MAIN_AGENT_ID}
                    )
            for frame in frames:
                self._apply_activity(projection, frame)
            if not projection.agents:
                raise KimiServerProtocolError("activity subscription delivered no agent activity")
        snapshot = projection.snapshot()
        for task in snapshot.tasks:
            self._task_owners[(session_id, task.id)] = task.owner_agent_id
        return snapshot

    def _apply_activity(
        self, projection: ActivityProjection, frame: dict[str, Any]
    ) -> bool:
        changed = projection.apply(frame)
        if changed:
            for task in projection.tasks.values():
                self._task_owners[(projection.session_id, task.id)] = (
                    task.owner_agent_id
                )
        return changed

    async def _subscribe_activity_agents(
        self,
        ws: Any,
        session_id: str,
        frames: list[dict[str, Any]],
        known_agent_ids: set[str],
    ) -> list[dict[str, Any]]:
        combined = list(frames)
        while True:
            referenced = _referenced_agent_ids(combined)
            added = sorted(referenced - known_agent_ids)
            if not added:
                return combined
            _, initial = await self._subscribe(ws, session_id, added)
            known_agent_ids.update(added)
            combined.extend(initial)

    async def background_task(self, session_id: str, task_id: str) -> BackgroundOutcome:
        activity = await self.inspect_activity(session_id)
        before = next((task for task in activity.tasks if task.id == task_id), None)
        return await self._background_task(session_id, task_id, before)

    async def _background_task(self, session_id: str, task_id: str, before: TaskInfo | None) -> BackgroundOutcome:
        try:
            value, _ = await self._task_command(session_id, task_id, "task.background")
        except KimiServerAPIError as exc:
            if exc.code == 40933:
                return "unsupported"
            raise
        task = _task_info_from_wire(value, session_id=session_id)
        if task.status != "running":
            return "ended"
        return "already_background" if before is not None and before.detached else "backgrounded"

    async def background_agent(self, session_id: str, agent_id: str) -> BackgroundOutcome:
        activity = await self.inspect_activity(session_id)
        agent = next((agent for agent in activity.agents if agent.id == agent_id), None)
        if agent is None or agent.task is None:
            return "unsupported"
        return await self._background_task(session_id, agent.task.id, agent.task)

    async def get_task(
        self, session_id: str, task_id: str, *, output_bytes: int = 8192
    ) -> TaskInfo:
        """Inspect a known task, including ended tasks, and its bounded output tail."""

        if not 0 <= output_bytes <= 1048576:
            raise ValueError("output_bytes must be between 0 and 1048576")
        task, output = await self._task_command(
            session_id, task_id, "task.watch", output_bytes=output_bytes
        )
        info = _task_info_from_wire(task, session_id=session_id, owner_agent_id=self._task_owners.get((session_id, task_id), _MAIN_AGENT_ID))
        if output.end is not None and (output.end > 0 or not info.output_preview):
            info = replace(info, output_preview=output.text, output_bytes=output.end)
        elif info.output_preview is not None:
            preview = info.output_preview.encode("utf-8")
            info = replace(
                info,
                output_preview=(
                    preview[-output_bytes:].decode("utf-8", errors="ignore")
                    if output_bytes
                    else ""
                ),
            )
        return info

    async def cancel_task(self, session_id: str, task_id: str) -> bool:
        """Stop a task directly and return as soon as the server acknowledges it."""

        await self._task_command(session_id, task_id, "task.stop")
        return True

    async def list_skills(self, session_id: str) -> list[SkillInfo]:
        data = await self._request_operation(
            "list_skills", path_parameters={"session_id": session_id}
        )
        return [_skill_info_from_wire(item) for item in data["skills"]]

    async def activate_skill(
        self, session_id: str, skill_name: str, *, args: str = ""
    ) -> str:
        payload = {"args": args} if args else {}
        data = await self._request_operation(
            "activate_skill",
            path_parameters={
                "session_id": session_id,
                "skill_name": skill_name,
            },
            json_body=payload,
        )
        if not data["activated"]:
            raise KimiServerProtocolError("kimi server did not activate the skill")
        return str(data["skill_name"])

    async def list_tools(self, session_id: str) -> list[ToolInfo]:
        data = await self._request_operation(
            "list_tools", params={"session_id": session_id}
        )
        return [_tool_info_from_wire(item) for item in data["tools"]]

    async def resolve_approval(
        self, session_id: str, approval_id: str, decision: ApprovalDecision
    ) -> InteractionResolution:
        return await self._resolve_interaction(
            session_id,
            approval_id,
            {"kind": "approval", "decision": decision},
            outcome=decision,
        )

    async def resolve_question(
        self, session_id: str, question_id: str, answers: tuple[QuestionAnswer, ...]
    ) -> InteractionResolution:
        return await self._resolve_interaction(
            session_id,
            question_id,
            {
                "kind": "question",
                "answers": {
                    answer.question_id: _question_answer_to_wire(answer)
                    for answer in answers
                },
                "method": "click",
            },
            outcome="answered",
        )

    async def dismiss_question(
        self, session_id: str, question_id: str
    ) -> InteractionResolution:
        return await self._resolve_interaction(
            session_id,
            question_id,
            {"kind": "question", "dismissed": True},
            outcome="dismissed",
        )

    async def _resolve_interaction(
        self,
        session_id: str,
        interaction_id: str,
        body: dict[str, Any],
        *,
        outcome: str,
    ) -> InteractionResolution:
        try:
            data = await self._request_operation(
                "respond_interaction",
                path_parameters={
                    "session_id": session_id,
                    "interaction_id": interaction_id,
                },
                json_body=body,
            )
        except KimiServerAPIError as exc:
            if exc.code in {40401, 40404, 40902}:
                return InteractionResolution.EXPIRED
            raise
        if not isinstance(data, dict) or data.get("outcome") != outcome:
            raise KimiServerProtocolError("invalid interaction response outcome")
        return InteractionResolution.APPLIED

    async def abort_prompt(self, session_id: str) -> bool:
        prompts = await self._request_operation(
            "list_prompts", path_parameters={"session_id": session_id}
        )
        active = prompts["active"]
        if active is None:
            return False
        return await self._abort_prompt_by_id(
            session_id, str(active["prompt_id"])
        )

    async def abort_session(self, session_id: str) -> bool:
        """Abort the main turn after discarding prompt work queued behind it."""

        prompts = await self._request_operation(
            "list_prompts", path_parameters={"session_id": session_id}
        )
        active = prompts["active"]
        selected = [*prompts["queued"], *([active] if active is not None else [])]
        for prompt in selected:
            try:
                await self._abort_prompt_by_id(
                    session_id, str(prompt["prompt_id"])
                )
            except KimiServerAPIError as exc:
                # A prompt can leave the queue after the snapshot; session work still needs stopping.
                if exc.code != 40402:
                    raise
        await self._request_operation(
            "abort_session",
            path_parameters={"session_id": session_id},
            json_body={},
        )
        return True

    async def _abort_prompt_by_id(
        self, session_id: str, prompt_id: str
    ) -> bool:
        try:
            data = await self._request_operation(
                "abort_prompt",
                path_parameters={
                    "session_id": session_id,
                    "prompt_id": prompt_id,
                },
            )
        except KimiServerAPIError as exc:
            if exc.code == 40903:
                return False
            raise
        return bool(data["aborted"])

    async def get_snapshot(self, session_id: str, *, agent_id: str = _MAIN_AGENT_ID) -> dict[str, Any]:
        """Read an authoritative native turns page."""

        page = await self._turns_page(session_id, agent_id, page_size=100)
        return snapshot_from_turns(page)

    async def _turns_page(
        self,
        session_id: str,
        agent_id: str,
        *,
        before_ordinal: int | None = None,
        page_size: int = 100,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"page_size": page_size}
        if before_ordinal is not None:
            params["before_ordinal"] = before_ordinal
        return await self._request_operation(
            "turns",
            path_parameters={"session_id": session_id, "agent_id": agent_id},
            params=params,
        )

    async def _turn_pages(
        self, session_id: str, agent_id: str, *, page_size: int = 100
    ) -> AsyncIterator[dict[str, Any]]:
        before_ordinal: int | None = None
        while True:
            page = await self._turns_page(
                session_id,
                agent_id,
                before_ordinal=before_ordinal,
                page_size=page_size,
            )
            turns = page.get("turns")
            if not isinstance(turns, list):
                raise KimiServerProtocolError("turns page must carry a turn list")
            yield page
            if not page.get("has_more"):
                return
            if not turns:
                raise KimiServerProtocolError("turns pagination made no progress")
            next_ordinal = min(turn["ordinal"] for turn in turns)
            if before_ordinal is not None and next_ordinal >= before_ordinal:
                raise KimiServerProtocolError("turns pagination made no progress")
            before_ordinal = next_ordinal

    async def _terminal_turn_for_prompt(
        self, session_id: str, agent_id: str, prompt_id: str | None
    ) -> dict[str, Any] | None:
        if prompt_id is None:
            return None
        async for page in self._turn_pages(session_id, agent_id, page_size=200):
            terminal = next(
                (
                    turn
                    for turn in reversed(page["turns"])
                    if turn_prompt_id(turn) == prompt_id
                    and turn.get("status") == "ended"
                ),
                None,
            )
            if terminal is not None:
                return terminal
        return None

    async def wait_until_subscribed(
        self, session_id: str, *, timeout: float = 15.0
    ) -> None:
        """Wait until ``subscribe_events`` has received its subscribe response."""

        ready = self._subscription_ready.setdefault(session_id, asyncio.Event())
        await asyncio.wait_for(ready.wait(), timeout)

    async def probe_subscription(self, session_id: str) -> None:
        """Subscribe once without inference and verify the native burst."""

        connection = await self._connection_info()
        try:
            async with asyncio.timeout(self._timeout):
                async with self._ws_connect(
                    _websocket_url(connection.base_url),
                    additional_headers={
                        "Authorization": f"Bearer {connection.token}"
                    },
                    ping_interval=None,
                    max_size=None,
                ) as ws:
                    _, frames = await self._subscribe(
                        ws, session_id, [_MAIN_AGENT_ID]
                    )
                    frame_types = {frame.get("type") for frame in frames}
                    burst = {"task.list", "agent", "usage", "goal", "todo", "turns"}
                    if not burst.issubset(frame_types):
                        raise KimiServerProtocolError(
                            "subscription response was not followed by the agent "
                            "state burst and authoritative turns page"
                        )
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
            raise KimiServerTransportError(
                f"kimi server probe subscription failed: {type(exc).__name__}"
            ) from exc

    async def subscribe_events(
        self, session_id: str, *, include_activity: bool = False, agent_id: str = _MAIN_AGENT_ID
    ) -> AsyncIterator[dict[str, Any] | InteractionUpdate | InteractionSnapshot | ActivityUpdate]:
        """Yield semantic events and recover from authoritative turns pages."""

        key = (session_id, agent_id)
        ready = self._subscription_ready.setdefault(session_id, asyncio.Event()) if agent_id == _MAIN_AGENT_ID else asyncio.Event()
        projection = ConversationProjection(session_id)
        activity_agent_ids: set[str] = set()
        reconnect_delay = self._reconnect_initial_backoff
        subscribed_once = False
        consecutive_response_mismatches = 0

        async with self._subscription_locks.setdefault(key, asyncio.Lock()):
            while not self._closed:
                ready.clear()
                connection = await self._connection_info()
                ws_url = _websocket_url(connection.base_url)
                try:
                    async with self._ws_connect(
                        ws_url,
                        additional_headers={
                            "Authorization": f"Bearer {connection.token}"
                        },
                        ping_interval=None,
                        max_size=None,
                    ) as ws:
                        self._active_ws[key] = ws
                        activity_agent_ids = {agent_id}
                        _, pending_frames = await self._subscribe(
                            ws, session_id, [agent_id]
                        )
                        if include_activity:
                            pending_frames = await self._subscribe_activity_agents(
                                ws,
                                session_id,
                                pending_frames,
                                activity_agent_ids,
                            )

                        # Every subscription replays the currently pending
                        # interactions, including dynamically discovered agents.
                        interaction_snapshot = InteractionSnapshot(
                            session_id, _pending_interaction_ids(pending_frames)
                        )
                        activity = ActivityProjection(session_id)
                        if agent_id == _MAIN_AGENT_ID:
                            self._activities[session_id] = activity
                        for frame in pending_frames:
                            self._apply_activity(activity, frame)
                            if (
                                frame.get("session_id") == session_id
                                and frame.get("agent_id") == _MAIN_AGENT_ID
                            ):
                                if frame.get("type") == "agent":
                                    self._agent_frames[session_id] = dict(frame)
                                elif frame.get("type") == "usage":
                                    self._usage_frames[session_id] = dict(frame)
                        ready.set()
                        subscribed_once = True
                        consecutive_response_mismatches = 0
                        reconnect_delay = self._reconnect_initial_backoff
                        yield interaction_snapshot
                        if include_activity:
                            yield ActivityUpdate(activity.snapshot(), baseline=True)
                        frame_queue: list[dict[str, Any]] = list(pending_frames)
                        initial_frames_remaining = len(frame_queue)
                        while not self._closed:
                            initial_frame = initial_frames_remaining > 0
                            if frame_queue:
                                frame = frame_queue.pop(0)
                                if initial_frame:
                                    initial_frames_remaining -= 1
                            else:
                                frame = await self._receive_json(ws)
                            frame_type = frame.get("type")
                            if self._apply_activity(activity, frame):
                                if include_activity and not initial_frame:
                                    yield ActivityUpdate(activity.snapshot())
                            if include_activity:
                                known_agent_ids = set(activity_agent_ids)
                                discovered = await self._subscribe_activity_agents(
                                    ws, session_id, [frame], activity_agent_ids
                                )
                                if activity_agent_ids != known_agent_ids:
                                    yield InteractionSnapshot(
                                        session_id,
                                        _pending_interaction_ids(discovered),
                                    )
                                frame_queue[0:0] = discovered[1:]
                            if frame_type == "ping":
                                await self._send_pong(ws)
                                continue
                            if frame_type == "interaction":
                                if frame.get("session_id") == session_id:
                                    yield _interaction_update_from_wire(frame)
                                continue
                            if frame_type == "turns.failed":
                                raise KimiServerAPIError(
                                    frame.get("code", -1),
                                    str(frame.get("msg") or "conversation load failed"),
                                )
                            if frame_type in _CONVERSATION_FRAME_TYPES:
                                if frame.get("agent_id") != agent_id:
                                    continue
                                events = projection.apply_frame(frame)
                                if any(
                                    event["payload"]["type"]
                                    in {
                                        "compaction.completed",
                                        "compaction.blocked",
                                        "compaction.cancelled",
                                    }
                                    for event in events
                                    if event["type"] == "session_event"
                                ):
                                    self._manual_compactions.discard(session_id)
                                for event in events:
                                    yield event
                                continue
                            if frame_type == "agent":
                                if frame.get("session_id") == session_id and (
                                    frame.get("agent_id") == _MAIN_AGENT_ID
                                ):
                                    self._agent_frames[session_id] = dict(frame)
                                if frame.get("agent_id") == agent_id:
                                    for event in projection.apply_agent(
                                        frame,
                                        manual=session_id in self._manual_compactions,
                                    ):
                                        yield event
                                continue
                            if frame_type == "usage":
                                if frame.get("session_id") == session_id and (
                                    frame.get("agent_id") == _MAIN_AGENT_ID
                                ):
                                    self._usage_frames[session_id] = dict(frame)
                                continue
                            if isinstance(frame_type, str):
                                LOGGER.debug(
                                    "ignoring unexpected WebSocket frame type %r",
                                    frame_type,
                                )
                                continue
                            raise KimiServerProtocolError(
                                f"WebSocket frame has no type: {frame!r}"
                            )
                except InvalidStatus as exc:
                    raise KimiServerProtocolError(
                        "kimi server rejected the WebSocket upgrade; check the "
                        "bearer token"
                    ) from exc
                except (
                    _WebSocketResponseMismatch,
                    ConnectionClosed,
                    KimiServerTransportError,
                    OSError,
                    TimeoutError,
                ) as exc:
                    if self._closed:
                        break
                    if isinstance(exc, _WebSocketResponseMismatch):
                        if not subscribed_once:
                            raise KimiServerOperationError(
                                f"Kimi subscription failed: {exc}"
                            ) from exc
                        consecutive_response_mismatches += 1
                        if (
                            consecutive_response_mismatches
                            >= _MAX_CONSECUTIVE_RESPONSE_MISMATCHES
                        ):
                            raise KimiServerProtocolError(
                                "kimi server repeatedly sent mismatched WebSocket "
                                "responses while reconnecting"
                            ) from exc
                    LOGGER.warning(
                        "kimi server WebSocket disconnected (%s); reconnecting "
                        "in %.2fs",
                        type(exc).__name__,
                        reconnect_delay,
                    )
                finally:
                    ready.clear()
                    self._active_ws.pop(key, None)
                    if agent_id == _MAIN_AGENT_ID:
                        self._activities.pop(session_id, None)
                        self._agent_frames.pop(session_id, None)
                        self._usage_frames.pop(session_id, None)
                if self._closed:
                    break
                await self._sleep(reconnect_delay)
                reconnect_delay = min(reconnect_delay * 2, self._reconnect_max_backoff)

    @asynccontextmanager
    async def _task_socket(self) -> AsyncIterator[Any]:
        connection = await self._connection_info()
        try:
            async with self._ws_connect(
                _websocket_url(connection.base_url),
                additional_headers={"Authorization": f"Bearer {connection.token}"},
                ping_interval=None,
                max_size=None,
                open_timeout=self._timeout,
            ) as ws:
                yield ws
        except (OSError, TimeoutError, ConnectionClosed, InvalidStatus) as exc:
            raise KimiServerTransportError(
                f"kimi server task exchange failed: {type(exc).__name__}"
            ) from exc

    async def _task_command(
        self, session_id: str, task_id: str, command: str, *, output_bytes: int = 0
    ) -> tuple[dict[str, Any], TaskOutputTail]:
        owner = self._task_owners.get((session_id, task_id), _MAIN_AGENT_ID)
        try:
            return await self._task_exchange(
                session_id, task_id, command, output_bytes=output_bytes
            )
        except KimiServerAPIError as exc:
            if exc.code != 40406 or owner != _MAIN_AGENT_ID:
                raise
            if (session_id, task_id) not in self._task_owners:
                await self.inspect_activity(session_id)
            if (
                self._task_owners.get((session_id, task_id), _MAIN_AGENT_ID)
                == _MAIN_AGENT_ID
            ):
                raise
        return await self._task_exchange(
            session_id, task_id, command, output_bytes=output_bytes
        )

    async def _task_exchange(
        self, session_id: str, task_id: str, command: str, *, output_bytes: int = 0
    ) -> tuple[dict[str, Any], TaskOutputTail]:
        request_id = uuid.uuid4().hex
        request = {
            "type": command,
            "request_id": request_id,
            "session_id": session_id,
            "agent_id": self._task_owners.get((session_id, task_id), _MAIN_AGENT_ID),
            "task_id": task_id,
        }
        if command == "task.watch":
            request["tail_bytes"] = max(1, output_bytes)
        output = TaskOutputTail(output_bytes)
        async with self._task_socket() as ws:
            async with asyncio.timeout(self._timeout):
                await self._send_json(ws, request)
                while True:
                    frame = await self._receive_task_frame(ws)
                    if self._is_task_output(frame, session_id, task_id):
                        output.append(frame)
                    if (
                        frame.get("type") == "response"
                        and frame.get("request_id") == request_id
                    ):
                        if frame.get("code") != 0:
                            raise KimiServerAPIError(
                                frame.get("code", -1),
                                str(frame.get("msg") or "task command failed"),
                            )
                        task = frame.get("task")
                        if not isinstance(task, dict) or task.get("task_id") != task_id:
                            raise KimiServerProtocolError(
                                "task response has no matching task"
                            )
                        break
            if command == "task.watch" and output_bytes and output.end is None:
                # The initial tail may precede or follow the response.
                # Empty/unavailable task logs need not produce an output frame.
                try:
                    async with asyncio.timeout(
                        min(self._timeout, _TASK_OUTPUT_GRACE_SECONDS)
                    ):
                        while output.end is None:
                            frame = await self._receive_task_frame(ws)
                            if self._is_task_output(frame, session_id, task_id):
                                output.append(frame)
                except (TimeoutError, ConnectionClosed, OSError):
                    pass
        return task, output

    async def _receive_task_frame(self, ws: Any) -> dict[str, Any]:
        while True:
            frame = await self._receive_json(ws)
            if frame.get("type") == "ping":
                await self._send_pong(ws)
                continue
            return frame

    def _is_task_output(self, frame: dict[str, Any], session_id: str, task_id: str) -> bool:
        return (
            frame.get("type") == "task.output"
            and frame.get("session_id") == session_id
            and frame.get("agent_id") == self._task_owners.get((session_id, task_id), _MAIN_AGENT_ID)
            and frame.get("task_id") == task_id
        )

    async def _request_operation(
        self,
        operation_name: str,
        *,
        path_parameters: dict[str, str] | None = None,
        json_body: Any = None,
        form_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        connection: ServerConnection | None = None,
        accepted_codes: frozenset[int] = frozenset(),
        raw_response: bool = False,
    ) -> Any:
        """Execute one operation from the tracked semantic contract."""

        operation = KIMI_REST_OPERATIONS[operation_name]
        quoted_parameters = {
            name: quote(value, safe="")
            for name, value in (path_parameters or {}).items()
        }
        try:
            path = operation.runtime_path.format_map(quoted_parameters)
        except KeyError as exc:
            raise KimiServerProtocolError(
                f"missing path parameter {exc.args[0]!r} for {operation_name}"
            ) from exc
        return await self._request(
            operation.method,
            path,
            json_body=json_body,
            form_body=form_body,
            files=files,
            params=params,
            connection=connection,
            accepted_codes=accepted_codes,
            raw_response=raw_response,
            api_version=operation.api_version,
        )

    async def _request_document(
        self,
        path: str,
        *,
        connection: ServerConnection | None = None,
    ) -> dict[str, Any]:
        if connection is None:
            connection = await self._connection_info()
        response = await self._send_http_request(
            "GET",
            f"{connection.base_url}{path}",
            headers={"Authorization": f"Bearer {connection.token}"},
            operation=path,
        )
        document = response.json()
        if not isinstance(document, dict):
            raise KimiServerProtocolError(f"{path} did not return a JSON object")
        return document

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        form_body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        connection: ServerConnection | None = None,
        accepted_codes: frozenset[int] = frozenset(),
        raw_response: bool = False,
        api_version: str = "v1",
    ) -> Any:
        if connection is None:
            connection = await self._connection_info()
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {connection.token}"}
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if form_body is not None:
            kwargs["data"] = form_body
        if files is not None:
            kwargs["files"] = files
        if params is not None:
            kwargs["params"] = params
        response = await self._send_http_request(
            method,
            f"{connection.base_url}/api/{api_version}{path}",
            operation=path,
            **kwargs,
        )
        if raw_response and "content-disposition" in response.headers:
            return response
        envelope = response.json()
        if envelope["code"] != 0 and envelope["code"] not in accepted_codes:
            raise KimiServerAPIError(
                envelope["code"],
                str(envelope.get("msg", "unknown error")),
                request_id=envelope.get("request_id"),
                details=envelope.get("details"),
            )
        if raw_response:
            raise KimiServerProtocolError("download returned an unexpected JSON response")
        return envelope["data"]

    async def _send_http_request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        **kwargs: Any,
    ) -> Any:
        try:
            response = await self._http.request(method, url, **kwargs)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KimiServerTransportError(
                f"kimi server {method} {operation} failed: {type(exc).__name__}"
            ) from exc
        return response

    async def _connection_info(self) -> ServerConnection:
        if self._supervisor is not None:
            return await self._supervisor.wait_until_ready()
        assert self._fixed_connection is not None
        return self._fixed_connection

    async def _subscribe(
        self, ws: Any, session_id: str, agent_ids: list[str]
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if not agent_ids:
            raise ValueError("a conversation subscription requires at least one agent")
        request_id = uuid.uuid4().hex
        await self._send_json(
            ws,
            {
                "type": "subscribe",
                "request_id": request_id,
                "session_id": session_id,
                "agent_ids": agent_ids,
            },
        )
        response: dict[str, Any] | None = None
        pending_frames: list[dict[str, Any]] = []
        remaining = set(agent_ids)
        while remaining:
            frame = await self._receive_json(ws)
            frame_type = frame.get("type")
            if frame_type == "ping":
                await self._send_pong(ws)
                continue
            if frame_type == "response":
                if frame.get("request_id") != request_id:
                    raise _WebSocketResponseMismatch(
                        f"expected response for request {request_id!r}, got "
                        f"{frame.get('request_id')!r}"
                    )
                if frame.get("code") != 0:
                    raise KimiServerAPIError(
                        frame.get("code", -1),
                        str(frame.get("msg") or "subscription failed"),
                        request_id=request_id,
                    )
                response = frame
                continue
            if response is None:
                if (
                    frame.get("agent_id") in remaining
                    and frame_type
                    in {
                        "turns",
                        "turns.failed",
                        "task.list",
                        "agent",
                        "usage",
                        "goal",
                        "todo",
                    }
                ):
                    raise _WebSocketResponseMismatch(
                        "the subscription response did not arrive first"
                    )
                pending_frames.append(frame)
                continue
            pending_frames.append(frame)
            if frame_type == "turns.failed" and frame.get("agent_id") in remaining:
                raise KimiServerAPIError(
                    frame.get("code", -1),
                    str(frame.get("msg") or "conversation load failed"),
                )
            if frame_type == "turns" and frame.get("agent_id") in remaining:
                remaining.remove(frame["agent_id"])
        assert response is not None
        return response, pending_frames

    @staticmethod
    async def _receive_json(ws: Any) -> dict[str, Any]:
        raw_frame = await ws.recv()
        if isinstance(raw_frame, bytes):
            raw_frame = raw_frame.decode("utf-8")
        frame = json.loads(raw_frame)
        if not isinstance(frame, dict):
            raise KimiServerProtocolError("WebSocket frame must be a JSON object")
        return frame

    @staticmethod
    async def _send_json(ws: Any, frame: dict[str, Any]) -> None:
        await ws.send(json.dumps(frame, separators=(",", ":")))

    async def _send_pong(self, ws: Any) -> None:
        await self._send_json(ws, {"type": "pong"})


def _pending_interaction_ids(frames: list[dict[str, Any]]) -> frozenset[str]:
    identifiers: set[str] = set()
    for frame in frames:
        if frame.get("type") != "interaction" or frame.get("status") != "pending":
            continue
        identity = frame.get("interaction_id")
        if not isinstance(identity, str) or not identity:
            raise KimiServerProtocolError("invalid pending interaction ID")
        identifiers.add(identity)
    return frozenset(identifiers)


def _referenced_agent_ids(frames: list[dict[str, Any]]) -> set[str]:
    references: set[str] = set()
    items: list[dict[str, Any]] = []
    for frame in frames:
        frame_type = frame.get("type")
        if frame_type == "turns":
            for turn in frame.get("turns") or ():
                items.extend(turn.get("items") or ())
        elif frame_type in {"item.started", "item.ended"}:
            item = frame.get("item")
            if isinstance(item, dict):
                items.append(item)
        elif frame_type == "item.updated":
            changes = frame.get("set")
            if isinstance(changes, dict) and isinstance(
                changes.get("agent_refs"), list
            ):
                items.append(changes)
    for item in items:
        for reference in item.get("agent_refs") or ():
            agent_id = reference.get("agent_id")
            if isinstance(agent_id, str) and agent_id:
                references.add(agent_id)
    return references


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, KIMI_WEBSOCKET_PATH, "", ""))
