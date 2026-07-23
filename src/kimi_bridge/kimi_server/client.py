"""Typed REST and WebSocket client for the managed Kimi server."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
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
    ApprovalRequest,
    QuestionAnswer,
    QuestionRequest,
)
from .contract import (
    KIMI_ASYNCAPI_PATH,
    KIMI_OPENAPI_PATH,
    KIMI_REST_OPERATIONS,
    KIMI_WEBSOCKET_PATH,
)
from .events import _EventCursor, _advance_cursor, _cursor_from_mapping
from .supervisor import KimiServerSupervisor
from .types import (
    GoalControl,
    GoalInfo,
    KimiServerAPIError,
    KimiServerProtocolError,
    KimiServerStartupError,
    ModelInfo,
    PermissionMode,
    ServerConnection,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    TaskStatus,
    ToolInfo,
)
from .wire import (
    _approval_request_from_wire,
    _goal_info_from_wire,
    _model_info_from_wire,
    _question_answer_to_wire,
    _question_request_from_wire,
    _session_profile_from_wire,
    _session_status_from_wire,
    _skill_info_from_wire,
    _task_info_from_wire,
    _tool_info_from_wire,
)


LOGGER = logging.getLogger(__name__)
_MAIN_AGENT_ID = "main"


class KimiServerClient:
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
        self._owns_http = http_client is None
        self._ws_connect = ws_connect
        self._sleep = sleep
        self._reconnect_initial_backoff = reconnect_initial_backoff
        self._reconnect_max_backoff = reconnect_max_backoff
        self._subscription_lock = asyncio.Lock()
        self._subscription_ready: dict[str, asyncio.Event] = {}
        self._usage_totals: dict[str, SessionUsage] = {}
        self._active_ws: Any | None = None
        self._closed = False

    async def __aenter__(self) -> KimiServerClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._active_ws is not None:
            await self._active_ws.close()
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

    async def get_server_version(self) -> str:
        """Return the managed server's advertised version."""

        metadata = await self.meta()
        server_version = metadata.get("server_version")
        if not isinstance(server_version, str) or not server_version:
            raise KimiServerProtocolError(
                "kimi server metadata has no server_version"
            )
        return server_version

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

    async def create_session(
        self,
        workspace: str,
        *,
        title: str | None = None,
        **profile: Any,
    ) -> str:
        payload: dict[str, Any] = {
            "metadata": {"cwd": str(Path(workspace).expanduser().resolve())}
        }
        if title is not None:
            payload["title"] = title
        if profile:
            payload["agent_config"] = profile
        data = await self._request_operation(
            "create_session", json_body=payload
        )
        return str(data["id"])

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request_operation(
            "get_session", path_parameters={"session_id": session_id}
        )

    async def get_session_profile(self, session_id: str) -> SessionProfile:
        data = await self._request_operation(
            "get_profile", path_parameters={"session_id": session_id}
        )
        return _session_profile_from_wire(data)

    async def get_session_status(self, session_id: str) -> SessionStatus:
        data = await self._request_operation(
            "session_status", path_parameters={"session_id": session_id}
        )
        return _session_status_from_wire(data)

    async def get_session_usage(self, session_id: str) -> SessionUsage:
        status = await self.get_session_status(session_id)
        totals = self._usage_totals.get(session_id)
        return SessionUsage(
            input_tokens=(totals.input_tokens if totals is not None else None),
            output_tokens=(totals.output_tokens if totals is not None else None),
            cache_read_tokens=(
                totals.cache_read_tokens if totals is not None else None
            ),
            cache_creation_tokens=(
                totals.cache_creation_tokens if totals is not None else None
            ),
            context_tokens=status.context_tokens,
            context_limit=status.context_limit,
        )

    async def compact_session(self, session_id: str) -> None:
        """Start manual context compaction for an idle session."""

        await self._request_operation(
            "compact_session",
            path_parameters={"session_id": session_id},
            json_body={},
        )

    async def undo_session(self, session_id: str, *, count: int = 1) -> None:
        """Undo the requested number of public session history steps."""

        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("undo count must be a positive integer")
        await self._request_operation(
            "undo_session",
            path_parameters={"session_id": session_id},
            json_body={"count": count},
        )

    async def get_goal(self, session_id: str) -> GoalInfo | None:
        """Return the session's current goal, if any."""

        data = await self._request_operation(
            "goal", path_parameters={"session_id": session_id}
        )
        if data is None:
            return None
        return _goal_info_from_wire(data)

    async def list_sessions(
        self,
        *,
        busy: bool = False,
        include_archive: bool = False,
        exclude_empty: bool = False,
        archived_only: bool = False,
        page_size: int | None = None,
        before_id: str | None = None,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "busy": busy,
            "include_archive": include_archive,
            "exclude_empty": exclude_empty,
            "archived_only": archived_only,
        }
        if page_size is not None:
            params["page_size"] = page_size
        if before_id is not None:
            params["before_id"] = before_id
        if after_id is not None:
            params["after_id"] = after_id
        data = await self._request_operation("list_sessions", params=params)
        return list(data["items"])

    async def submit_prompt(
        self,
        session_id: str,
        content: str | list[dict[str, Any]],
        *,
        model: str | None = None,
        thinking: str | None = None,
        permission_mode: PermissionMode | None = None,
        plan_mode: bool | None = None,
    ) -> dict[str, Any]:
        content_items = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        payload: dict[str, Any] = {"content": content_items}
        if model is not None:
            payload["model"] = model
        if thinking is not None:
            payload["thinking"] = thinking
        if permission_mode is not None:
            payload["permission_mode"] = permission_mode
        if plan_mode is not None:
            payload["plan_mode"] = plan_mode
        return await self._request_operation(
            "submit_prompt",
            path_parameters={"session_id": session_id},
            json_body=payload,
        )

    async def steer_prompts(self, session_id: str, prompt_ids: list[str]) -> bool:
        data = await self._request_operation(
            "steer_prompts",
            path_parameters={"session_id": session_id},
            json_body={"prompt_ids": prompt_ids},
        )
        return bool(data["steered"])

    async def update_profile(
        self,
        session_id: str,
        *,
        title: str | None = None,
        model: str | None = None,
        thinking: str | None = None,
        permission_mode: PermissionMode | None = None,
        plan_mode: bool | None = None,
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
        params = {"status": status} if status is not None else None
        data = await self._request_operation(
            "list_tasks",
            path_parameters={"session_id": session_id},
            params=params,
        )
        return [_task_info_from_wire(item) for item in data["items"]]

    async def get_task(
        self,
        session_id: str,
        task_id: str,
        *,
        output_bytes: int = 8192,
    ) -> TaskInfo:
        if output_bytes < 0:
            raise ValueError("output_bytes must be non-negative")
        data = await self._request_operation(
            "get_task",
            path_parameters={"session_id": session_id, "task_id": task_id},
            params={"with_output": True, "output_bytes": output_bytes},
        )
        return _task_info_from_wire(data)

    async def cancel_task(self, session_id: str, task_id: str) -> bool:
        data = await self._request_operation(
            "cancel_task",
            path_parameters={"session_id": session_id, "task_id": task_id},
        )
        return bool(data["cancelled"])

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

    async def list_approvals(self, session_id: str) -> list[ApprovalRequest]:
        try:
            data = await self._request_operation(
                "list_approvals",
                path_parameters={"session_id": session_id},
                params={"status": "pending"},
            )
        except KimiServerAPIError as exc:
            if exc.code == 40001:
                return []
            raise
        return [_approval_request_from_wire(item) for item in data["items"]]

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        decision: ApprovalDecision,
    ) -> bool:
        data = await self._request_operation(
            "resolve_approval",
            path_parameters={
                "session_id": session_id,
                "approval_id": approval_id,
            },
            json_body={"decision": decision},
        )
        return bool(data["resolved"])

    async def list_questions(self, session_id: str) -> list[QuestionRequest]:
        try:
            data = await self._request_operation(
                "list_questions",
                path_parameters={"session_id": session_id},
                params={"status": "pending"},
            )
        except KimiServerAPIError as exc:
            if exc.code == 40001:
                return []
            raise
        return [_question_request_from_wire(item) for item in data["items"]]

    async def resolve_question(
        self,
        session_id: str,
        question_id: str,
        answers: tuple[QuestionAnswer, ...],
    ) -> bool:
        answers_payload = {
            answer.question_id: _question_answer_to_wire(answer)
            for answer in answers
        }
        data = await self._request_operation(
            "resolve_question",
            path_parameters={
                "session_id": session_id,
                "question_id": question_id,
            },
            json_body={"answers": answers_payload, "method": "click"},
        )
        return bool(data["resolved"])

    async def dismiss_question(self, session_id: str, question_id: str) -> bool:
        data = await self._request_operation(
            "dismiss_question",
            path_parameters={
                "session_id": session_id,
                "question_id": question_id,
            },
        )
        return bool(data["dismissed"])

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
        queued = prompts["queued"]
        for prompt in queued:
            await self._abort_prompt_by_id(
                session_id, str(prompt["prompt_id"])
            )
        active = prompts["active"]
        if active is not None:
            await self._abort_prompt_by_id(
                session_id, str(active["prompt_id"])
            )
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

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return await self._request_operation(
            "snapshot", path_parameters={"session_id": session_id}
        )

    async def wait_until_subscribed(
        self, session_id: str, *, timeout: float = 15.0
    ) -> None:
        """Wait until ``subscribe_events`` has received its subscribe ack."""

        ready = self._subscription_ready.setdefault(session_id, asyncio.Event())
        await asyncio.wait_for(ready.wait(), timeout)

    async def probe_subscription(self, session_id: str) -> None:
        """Materialize and subscribe once without submitting a prompt."""

        connection = await self._connection_info()
        await self._materialize_session(session_id, connection)
        async with self._ws_connect(
            _websocket_url(connection.base_url),
            additional_headers={
                "Authorization": f"Bearer {connection.token}"
            },
            ping_interval=None,
        ) as ws:
            await self._expect_server_hello(ws)
            await self._send_client_hello(ws)
            subscribe_ack, _pending_frames = await self._send_subscribe(
                ws, session_id, None
            )
            payload = subscribe_ack.get("payload")
            accepted = payload.get("accepted") if isinstance(payload, dict) else None
            if not isinstance(accepted, list) or session_id not in accepted:
                raise KimiServerProtocolError(
                    "credential-free probe session was not accepted by WebSocket"
                )

    async def subscribe_events(self, session_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield session events and reconnect from their cursor.

        Only one subscription generator may own the socket at a time. On a
        reconnect the last ``seq``/``epoch`` is sent as the subscription cursor.
        If the daemon reports an unusable cursor, or a gap/epoch change is seen,
        a REST snapshot establishes a fresh cursor before reconnecting. The
        snapshot is yielded in a ``resync_required`` notification so consumers
        can rebuild any derived presentation state that volatile deltas had
        updated.
        """

        ready = self._subscription_ready.setdefault(session_id, asyncio.Event())
        cursor: _EventCursor | None = None
        reconnect_delay = self._reconnect_initial_backoff

        async with self._subscription_lock:
            while not self._closed:
                ready.clear()
                # Usage totals are live-only WebSocket state. A reconnect may
                # have missed updates, so do not present the previous value as
                # current until the server publishes another total.
                self._usage_totals.pop(session_id, None)
                connection = await self._connection_info()
                ws_url = _websocket_url(connection.base_url)
                try:
                    await self._materialize_session(session_id, connection)
                    # Confirmed against kimi 0.28.1: WebSocket auth uses the
                    # same Bearer header as REST; no query token or hello field
                    # is required.
                    async with self._ws_connect(
                        ws_url,
                        additional_headers={
                            "Authorization": f"Bearer {connection.token}"
                        },
                        ping_interval=None,
                    ) as ws:
                        self._active_ws = ws
                        await self._expect_server_hello(ws)
                        await self._send_client_hello(ws)
                        subscribe_ack, pending_frames = await self._send_subscribe(
                            ws, session_id, cursor
                        )
                        ack_payload = subscribe_ack["payload"]

                        if session_id in ack_payload.get("not_found", []):
                            raise KimiServerProtocolError(
                                "session was not accepted after public-v1 "
                                f"materialization: {session_id!r}"
                            )
                        if session_id in ack_payload.get("resync_required", []):
                            cursor, resync_event = await self._snapshot_resync(
                                session_id, "subscription_cursor_rejected"
                            )
                            yield resync_event
                            continue
                        if session_id not in ack_payload.get("accepted", []):
                            raise KimiServerProtocolError(
                                f"server did not accept subscription for {session_id!r}"
                            )

                        if cursor is None:
                            acknowledged = ack_payload.get("cursors", {}).get(
                                session_id
                            )
                            if acknowledged is not None:
                                cursor = _cursor_from_mapping(acknowledged)

                        ready.set()
                        reconnect_delay = self._reconnect_initial_backoff
                        must_reconnect = False
                        while not self._closed:
                            if pending_frames:
                                frame = pending_frames.pop(0)
                            else:
                                frame = await self._receive_json(ws)
                            frame_type = frame.get("type")
                            if frame_type == "ping":
                                await self._send_pong(ws, frame)
                                continue
                            if frame_type == "resync_required":
                                payload = frame.get("payload", {})
                                if payload.get("session_id") == session_id:
                                    reason = payload.get("reason")
                                    cursor, resync_event = await self._snapshot_resync(
                                        session_id,
                                        reason
                                        if isinstance(reason, str)
                                        else "server_requested",
                                    )
                                    yield resync_event
                                    must_reconnect = True
                                    break
                                continue
                            if frame_type == "error":
                                self._raise_ws_error(frame)
                            if "seq" not in frame or "payload" not in frame:
                                LOGGER.debug(
                                    "ignoring unexpected WebSocket frame type %r",
                                    frame_type,
                                )
                                continue
                            if frame.get("session_id") not in (None, session_id):
                                continue

                            # Streaming deltas are volatile and reuse the
                            # surrounding persisted event's seq. They must be
                            # delivered, but must not advance the replay cursor.
                            if frame.get("volatile") is True:
                                event_epoch = frame.get("epoch")
                                if (
                                    cursor is not None
                                    and cursor.epoch is not None
                                    and event_epoch is not None
                                    and event_epoch != cursor.epoch
                                ):
                                    cursor, resync_event = await self._snapshot_resync(
                                        session_id, "epoch_changed"
                                    )
                                    yield resync_event
                                    must_reconnect = True
                                    break
                                self._record_session_usage(session_id, frame)
                                yield frame
                                continue

                            # This subscription intentionally receives only
                            # main-agent events. Sequence numbers are global to
                            # the session, so events from filtered subagents
                            # leave legitimate gaps in the delivered stream.
                            disposition = _advance_cursor(
                                cursor, frame, allow_sequence_gaps=True
                            )
                            if disposition == "duplicate":
                                continue
                            if disposition == "resync":
                                reason = (
                                    "epoch_changed"
                                    if cursor is not None
                                    and cursor.epoch is not None
                                    and frame.get("epoch") is not None
                                    and frame.get("epoch") != cursor.epoch
                                    else "sequence_gap"
                                )
                                cursor, resync_event = await self._snapshot_resync(
                                    session_id, reason
                                )
                                yield resync_event
                                must_reconnect = True
                                break
                            cursor = _EventCursor(
                                seq=int(frame["seq"]),
                                epoch=frame.get("epoch")
                                or (cursor.epoch if cursor is not None else None),
                            )
                            self._record_session_usage(session_id, frame)
                            yield frame

                        if must_reconnect:
                            continue
                except InvalidStatus as exc:
                    raise KimiServerProtocolError(
                        "kimi server rejected the WebSocket upgrade; check the "
                        "bearer token"
                    ) from exc
                except (ConnectionClosed, OSError, TimeoutError) as exc:
                    if self._closed:
                        break
                    LOGGER.warning(
                        "kimi server WebSocket disconnected (%s); reconnecting "
                        "in %.2fs",
                        type(exc).__name__,
                        reconnect_delay,
                    )
                    await self._sleep(reconnect_delay)
                    reconnect_delay = min(
                        reconnect_delay * 2, self._reconnect_max_backoff
                    )
                finally:
                    ready.clear()
                    self._active_ws = None

    async def _request_operation(
        self,
        operation_name: str,
        *,
        path_parameters: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
        connection: ServerConnection | None = None,
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
            params=params,
            connection=connection,
        )

    async def _request_document(
        self,
        path: str,
        *,
        connection: ServerConnection | None = None,
    ) -> dict[str, Any]:
        if connection is None:
            connection = await self._connection_info()
        response = await self._http.request(
            "GET",
            f"{connection.base_url}{path}",
            headers={"Authorization": f"Bearer {connection.token}"},
        )
        response.raise_for_status()
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
        params: dict[str, Any] | None = None,
        connection: ServerConnection | None = None,
    ) -> Any:
        if connection is None:
            connection = await self._connection_info()
        kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {connection.token}"}
        }
        if json_body is not None:
            kwargs["json"] = json_body
        if params is not None:
            kwargs["params"] = params
        response = await self._http.request(
            method, f"{connection.base_url}/api/v1{path}", **kwargs
        )
        response.raise_for_status()
        envelope = response.json()
        if envelope["code"] != 0:
            raise KimiServerAPIError(
                envelope["code"],
                str(envelope.get("msg", "unknown error")),
                request_id=envelope.get("request_id"),
                details=envelope.get("details"),
            )
        return envelope["data"]

    async def _materialize_session(
        self, session_id: str, connection: ServerConnection
    ) -> None:
        """Load a stored session into the current WebSocket registry."""

        await self._request_operation(
            "session_status",
            path_parameters={"session_id": session_id},
            connection=connection,
        )

    async def _connection_info(self) -> ServerConnection:
        if self._supervisor is not None:
            return await self._supervisor.wait_until_ready()
        assert self._fixed_connection is not None
        return self._fixed_connection

    async def _expect_server_hello(self, ws: Any) -> None:
        while True:
            frame = await self._receive_json(ws)
            if frame.get("type") == "ping":
                await self._send_pong(ws, frame)
                continue
            if frame.get("type") == "error":
                self._raise_ws_error(frame)
            if frame.get("type") != "server_hello":
                raise KimiServerProtocolError(
                    f"expected server_hello, got {frame.get('type')!r}"
                )
            return

    async def _send_client_hello(self, ws: Any) -> None:
        request_id = uuid.uuid4().hex
        await self._send_json(
            ws,
            {
                "type": "client_hello",
                "id": request_id,
                "payload": {
                    "client_id": "kimi-bridge",
                    "subscriptions": [],
                },
            },
        )
        await self._wait_for_ack(ws, request_id)

    async def _send_subscribe(
        self, ws: Any, session_id: str, cursor: _EventCursor | None
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        request_id = uuid.uuid4().hex
        payload: dict[str, Any] = {
            "session_ids": [session_id],
            "agent_filter": {session_id: [_MAIN_AGENT_ID]},
        }
        if cursor is not None:
            cursor_payload: dict[str, Any] = {"seq": cursor.seq}
            if cursor.epoch is not None:
                cursor_payload["epoch"] = cursor.epoch
            payload["cursors"] = {session_id: cursor_payload}
        await self._send_json(
            ws,
            {"type": "subscribe", "id": request_id, "payload": payload},
        )
        pending_frames: list[dict[str, Any]] = []
        ack = await self._wait_for_ack(ws, request_id, pending_frames=pending_frames)
        return ack, pending_frames

    async def _wait_for_ack(
        self,
        ws: Any,
        request_id: str,
        *,
        pending_frames: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        while True:
            frame = await self._receive_json(ws)
            if frame.get("type") == "ping":
                await self._send_pong(ws, frame)
                continue
            if frame.get("type") == "error":
                self._raise_ws_error(frame)
            if pending_frames is not None and "seq" in frame and "payload" in frame:
                pending_frames.append(frame)
                continue
            if frame.get("type") != "ack" or frame.get("id") != request_id:
                raise KimiServerProtocolError(
                    f"expected ack for request {request_id!r}"
                )
            if frame.get("code") != 0:
                raise KimiServerProtocolError(
                    f"WebSocket request failed: {frame.get('code')} "
                    f"{frame.get('msg', '')}".rstrip()
                )
            return frame

    async def _snapshot_resync(
        self, session_id: str, reason: str
    ) -> tuple[_EventCursor, dict[str, Any]]:
        snapshot = await self.get_snapshot(session_id)
        cursor = _EventCursor(
            seq=int(snapshot["as_of_seq"]), epoch=str(snapshot["epoch"])
        )
        LOGGER.warning(
            "resynced session %s from snapshot at seq %s",
            session_id,
            cursor.seq,
        )
        return cursor, {
            "type": "resync_required",
            "session_id": session_id,
            "payload": {
                "type": "resync_required",
                "session_id": session_id,
                "reason": reason,
            },
            "snapshot": snapshot,
        }

    def _record_session_usage(
        self, session_id: str, frame: dict[str, Any]
    ) -> None:
        payload = frame["payload"]
        if payload.get("type") != "agent.status.updated":
            return
        usage = payload.get("usage")
        if not isinstance(usage, dict) or "total" not in usage:
            return
        total = usage["total"]
        if not isinstance(total, dict):
            raise KimiServerProtocolError(
                "agent.status.updated usage total must be an object"
            )
        self._usage_totals[session_id] = SessionUsage(
            input_tokens=int(total["inputOther"]),
            output_tokens=int(total["output"]),
            cache_read_tokens=int(total["inputCacheRead"]),
            cache_creation_tokens=int(total["inputCacheCreation"]),
            context_tokens=None,
            context_limit=None,
        )

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

    async def _send_pong(self, ws: Any, ping: dict[str, Any]) -> None:
        nonce = ping.get("payload", {}).get("nonce")
        if not isinstance(nonce, str):
            raise KimiServerProtocolError("ping frame is missing its nonce")
        await self._send_json(ws, {"type": "pong", "payload": {"nonce": nonce}})

    @staticmethod
    def _raise_ws_error(frame: dict[str, Any]) -> None:
        payload = frame.get("payload", {})
        raise KimiServerProtocolError(
            f"WebSocket error {payload.get('code')}: {payload.get('msg', '')}".rstrip()
        )


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return urlunsplit((scheme, parsed.netloc, KIMI_WEBSOCKET_PATH, "", ""))
