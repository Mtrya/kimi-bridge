"""Supervise and talk to the local kimi server.

This module is the bridge's only boundary with kimi-code. It owns the child
process, bearer token, REST envelope, and WebSocket cursor protocol.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import socket
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import quote, urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

from .compatibility import (
    KimiExecutableIdentity,
    KimiProduct,
    KimiProductFingerprintError,
    VersionSupport,
    classify_kimi_code_version,
    identify_kimi_executable,
    legacy_product_message,
    normalize_kimi_code_version,
    unknown_version_warning,
)
from .interactions import (
    ApprovalDecision,
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


LOGGER = logging.getLogger(__name__)
_MAIN_AGENT_ID = "main"

_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_SERVER_URL_RE = re.compile(
    r"http://127\.0\.0\.1:(?P<port>[0-9]{1,5})/#token=(?P<token>[A-Za-z0-9_-]+)"
)
_TOKEN_RE = re.compile(r"(?<=#token=)[A-Za-z0-9_-]+")
_AUTH_ERROR_MARKERS = (
    "auth.login_required",
    "authentication required",
    "login required",
    "not logged in",
    "kimi login",
)


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


def parse_server_startup_line(line: str) -> tuple[int, str] | None:
    """Extract ``(port, token)`` from a kimi-server startup line.

    The stable contract is the loopback URL and fragment rather than its human
    label. ANSI styling is ignored.
    """

    plain_line = _ANSI_ESCAPE_RE.sub("", line)
    match = _SERVER_URL_RE.search(plain_line)
    if match is None:
        return None
    return int(match.group("port")), match.group("token")


def _redact_tokens(text: str) -> str:
    return _TOKEN_RE.sub("<redacted>", _ANSI_ESCAPE_RE.sub("", text))


def _pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class KimiServerSupervisor:
    """Run ``kimi web`` as a restartable foreground child process."""

    def __init__(
        self,
        *,
        preferred_port: int | None = None,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 5.0,
        initial_backoff: float = 1.0,
        max_backoff: float = 30.0,
        executable: str = "kimi",
        process_factory: Callable[..., Awaitable[Any]] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        port_picker: Callable[[], int] = _pick_free_port,
    ) -> None:
        if preferred_port is not None and not 1 <= preferred_port <= 65535:
            raise ValueError("preferred_port must be between 1 and 65535")
        if startup_timeout <= 0 or shutdown_timeout <= 0:
            raise ValueError("startup and shutdown timeouts must be positive")
        if initial_backoff < 0 or max_backoff < initial_backoff:
            raise ValueError("invalid restart backoff")

        self._preferred_port = preferred_port
        self._startup_timeout = startup_timeout
        self._shutdown_timeout = shutdown_timeout
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._executable = executable
        self._process_factory = process_factory or asyncio.create_subprocess_exec
        self._sleep = sleep
        self._port_picker = port_picker

        self._port: int | None = None
        self._process: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping = asyncio.Event()
        self._state_changed = asyncio.Condition()
        self._connection: ServerConnection | None = None
        self._failure: BaseException | None = None
        self._generation = 0
        self._executable_identity: KimiExecutableIdentity | None = None

    @property
    def connection(self) -> ServerConnection:
        """Return the current endpoint or fail if the child is not ready."""

        if self._connection is None:
            raise RuntimeError("kimi server is not ready")
        return self._connection

    @property
    def process(self) -> Any | None:
        """Expose the current process for diagnostics and manual crash tests."""

        return self._process

    @property
    def executable_identity(self) -> KimiExecutableIdentity:
        """Return the official Kimi executable identity established at preflight."""

        if self._executable_identity is None:
            raise RuntimeError("kimi executable preflight has not completed")
        return self._executable_identity

    async def __aenter__(self) -> KimiServerSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> ServerConnection:
        """Start supervision and wait for the first bearer token."""

        if self._task is not None:
            return await self.wait_until_ready()

        await self._check_executable_identity()
        self._port = self._preferred_port or self._port_picker()
        self._stopping.clear()
        self._failure = None
        self._task = asyncio.create_task(
            self._supervise(), name="kimi-server-supervisor"
        )
        try:
            return await self.wait_until_ready()
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Terminate the child and stop future restarts."""

        task = self._task
        if task is None:
            return

        self._stopping.set()
        await self._publish_connection(None)
        process = self._process
        if process is not None and process.returncode is None:
            process.terminate()

        try:
            await asyncio.wait_for(asyncio.shield(task), self._shutdown_timeout)
        except TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), self._shutdown_timeout)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            self._task = None
            self._process = None

    async def wait_until_ready(
        self, *, after_generation: int | None = None
    ) -> ServerConnection:
        """Wait for a ready child, optionally newer than a known generation."""

        async with self._state_changed:
            await self._state_changed.wait_for(
                lambda: (
                    self._failure is not None
                    or self._stopping.is_set()
                    or (
                        self._connection is not None
                        and (
                            after_generation is None
                            or self._connection.generation > after_generation
                        )
                    )
                )
            )
            if self._failure is not None:
                raise self._failure
            if self._connection is None:
                raise RuntimeError("kimi server supervisor is stopping")
            return self._connection

    async def _check_executable_identity(self) -> None:
        version_output = await self._run_cli_probe("--version")
        help_output = await self._run_cli_probe("--help")
        try:
            identity = identify_kimi_executable(version_output, help_output)
        except KimiProductFingerprintError as exc:
            raise KimiServerStartupError(str(exc)) from exc

        if identity.product is KimiProduct.LEGACY_KIMI_CLI:
            message = legacy_product_message(identity.version)
            LOGGER.warning("INCOMPATIBLE KIMI PRODUCT: %s", message)
            raise KimiServerStartupError(message)

        self._executable_identity = identity
        if identity.support is VersionSupport.SUPPORTED:
            LOGGER.info("kimi-code executable version: %s", identity.version)
        else:
            LOGGER.warning("%s", unknown_version_warning(identity.version))

    async def _run_cli_probe(self, *arguments: str) -> str:
        command_text = " ".join(("kimi", *arguments))
        try:
            process = await self._process_factory(
                self._executable,
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise KimiServerStartupError(
                "kimi-code is not installed or 'kimi' is not on PATH"
            ) from exc

        try:
            if process.stdout is None:
                raise KimiServerStartupError(
                    f"{command_text} stdout was not captured"
                )
            raw_output = await asyncio.wait_for(
                process.stdout.read(), self._startup_timeout
            )
            returncode = await asyncio.wait_for(
                process.wait(), self._startup_timeout
            )
        except TimeoutError as exc:
            await self._terminate_process(process)
            raise KimiServerStartupError(
                f"{command_text} did not finish within {self._startup_timeout:g}s"
            ) from exc
        except BaseException:
            await self._terminate_process(process)
            raise

        output = _redact_tokens(
            raw_output.decode("utf-8", errors="replace")
        ).strip()
        if returncode != 0:
            raise KimiServerStartupError(
                f"{command_text} exited with status {returncode}: "
                f"{output or 'no output'}"
            )
        return output

    async def _supervise(self) -> None:
        delay = self._initial_backoff
        try:
            while not self._stopping.is_set():
                returncode = await self._run_child()
                await self._publish_connection(None)
                if self._stopping.is_set():
                    break

                LOGGER.warning(
                    "kimi server exited unexpectedly with status %s; "
                    "restarting in %.1fs",
                    returncode,
                    delay,
                )
                await self._sleep(delay)
                delay = min(delay * 2, self._max_backoff)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            await self._publish_failure(exc)

    async def _run_child(self) -> int:
        assert self._port is not None
        command = (
            self._executable,
            "web",
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            str(self._port),
        )
        try:
            process = await self._process_factory(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise KimiServerStartupError(
                "kimi-code is not installed or 'kimi' is not on PATH"
            ) from exc

        self._process = process
        try:
            try:
                port, token = await asyncio.wait_for(
                    self._read_startup_credentials(process), self._startup_timeout
                )
            except TimeoutError as exc:
                raise KimiServerStartupError(
                    f"kimi web did not print its startup URL within "
                    f"{self._startup_timeout:g}s"
                ) from exc

            if port != self._port:
                raise KimiServerStartupError(
                    f"kimi server announced port {port}, expected {self._port}"
                )

            self._generation += 1
            await self._publish_connection(
                ServerConnection(
                    base_url=f"http://127.0.0.1:{port}",
                    port=port,
                    generation=self._generation,
                    token=token,
                )
            )
            LOGGER.info(
                "kimi server is ready on 127.0.0.1:%s (generation %s)",
                port,
                self._generation,
            )

            drain_task = asyncio.create_task(self._drain_output(process))
            returncode = await process.wait()
            await drain_task
            return int(returncode)
        except BaseException:
            await self._terminate_process(process)
            raise
        finally:
            if self._process is process:
                self._process = None

    async def _read_startup_credentials(self, process: Any) -> tuple[int, str]:
        if process.stdout is None:
            raise KimiServerStartupError("kimi server stdout was not captured")

        recent_output: list[str] = []
        while True:
            raw_line = await process.stdout.readline()
            if not raw_line:
                detail = " | ".join(recent_output[-5:]) or "no output"
                raise KimiServerStartupError(
                    "kimi server exited before printing its startup URL: " + detail
                )
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            safe_line = _redact_tokens(line)
            recent_output.append(safe_line)
            LOGGER.debug("kimi server: %s", safe_line)

            lowered = safe_line.lower()
            if any(marker in lowered for marker in _AUTH_ERROR_MARKERS):
                raise KimiServerAuthenticationError(
                    "kimi-code is not authenticated; run 'kimi login' and retry"
                )

            credentials = parse_server_startup_line(line)
            if credentials is not None:
                return credentials

    async def _drain_output(self, process: Any) -> None:
        if process.stdout is None:
            return
        while raw_line := await process.stdout.readline():
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            LOGGER.debug("kimi server: %s", _redact_tokens(line))

    async def _terminate_process(self, process: Any) -> None:
        if process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        except TimeoutError:
            process.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), self._shutdown_timeout)

    async def _publish_connection(self, connection: ServerConnection | None) -> None:
        async with self._state_changed:
            self._connection = connection
            self._state_changed.notify_all()

    async def _publish_failure(self, failure: BaseException) -> None:
        async with self._state_changed:
            self._failure = failure
            self._connection = None
            self._state_changed.notify_all()


@dataclass(slots=True)
class _EventCursor:
    seq: int
    epoch: str | None


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

    async def meta(self) -> dict[str, Any]:
        return await self._request("GET", "/meta")

    async def get_config(self) -> dict[str, Any]:
        """Return the server's resolved, secret-redacted configuration."""

        return await self._request("GET", "/config")

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

        data = await self._request("GET", "/models")
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
        data = await self._request("POST", "/sessions", json_body=payload)
        return str(data["id"])

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}")

    async def get_session_profile(self, session_id: str) -> SessionProfile:
        data = await self._request("GET", f"/sessions/{session_id}/profile")
        return _session_profile_from_wire(data)

    async def get_session_status(self, session_id: str) -> SessionStatus:
        data = await self._request("GET", f"/sessions/{session_id}/status")
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

        await self._request(
            "POST",
            f"/sessions/{session_id}:compact",
            json_body={},
        )

    async def undo_session(self, session_id: str, *, count: int = 1) -> None:
        """Undo the requested number of public session history steps."""

        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            raise ValueError("undo count must be a positive integer")
        await self._request(
            "POST",
            f"/sessions/{session_id}:undo",
            json_body={"count": count},
        )

    async def get_goal(self, session_id: str) -> GoalInfo | None:
        """Return the session's current goal, if any."""

        data = await self._request("GET", f"/sessions/{session_id}/goal")
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
        data = await self._request("GET", "/sessions", params=params)
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
        return await self._request(
            "POST",
            f"/sessions/{session_id}/prompts",
            json_body=payload,
        )

    async def steer_prompts(self, session_id: str, prompt_ids: list[str]) -> bool:
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/prompts:steer",
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
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/profile",
            json_body=payload,
        )
        return _session_profile_from_wire(data)

    async def list_tasks(
        self, session_id: str, *, status: TaskStatus | None = None
    ) -> list[TaskInfo]:
        params = {"status": status} if status is not None else None
        data = await self._request(
            "GET", f"/sessions/{session_id}/tasks", params=params
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
        data = await self._request(
            "GET",
            f"/sessions/{session_id}/tasks/{quote(task_id, safe='')}",
            params={"with_output": True, "output_bytes": output_bytes},
        )
        return _task_info_from_wire(data)

    async def cancel_task(self, session_id: str, task_id: str) -> bool:
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/tasks/{quote(task_id, safe='')}:cancel",
        )
        return bool(data["cancelled"])

    async def list_skills(self, session_id: str) -> list[SkillInfo]:
        data = await self._request("GET", f"/sessions/{session_id}/skills")
        return [_skill_info_from_wire(item) for item in data["skills"]]

    async def activate_skill(
        self, session_id: str, skill_name: str, *, args: str = ""
    ) -> str:
        payload = {"args": args} if args else {}
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/skills/{quote(skill_name, safe='')}:activate",
            json_body=payload,
        )
        if not data["activated"]:
            raise KimiServerProtocolError("kimi server did not activate the skill")
        return str(data["skill_name"])

    async def list_tools(self, session_id: str) -> list[ToolInfo]:
        data = await self._request(
            "GET", "/tools", params={"session_id": session_id}
        )
        return [_tool_info_from_wire(item) for item in data["tools"]]

    async def list_approvals(self, session_id: str) -> list[ApprovalRequest]:
        try:
            data = await self._request(
                "GET",
                f"/sessions/{session_id}/approvals",
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
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/approvals/{approval_id}",
            json_body={"decision": decision},
        )
        return bool(data["resolved"])

    async def list_questions(self, session_id: str) -> list[QuestionRequest]:
        try:
            data = await self._request(
                "GET",
                f"/sessions/{session_id}/questions",
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
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/questions/{question_id}",
            json_body={"answers": answers_payload, "method": "click"},
        )
        return bool(data["resolved"])

    async def dismiss_question(self, session_id: str, question_id: str) -> bool:
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/questions/{question_id}:dismiss",
            json_body={},
        )
        return bool(data["dismissed"])

    async def abort_prompt(self, session_id: str) -> bool:
        prompts = await self._request("GET", f"/sessions/{session_id}/prompts")
        active = prompts["active"]
        if active is None:
            return False
        prompt_id = str(active["prompt_id"])
        try:
            data = await self._request(
                "POST",
                f"/sessions/{session_id}/prompts/{prompt_id}:abort",
            )
        except KimiServerAPIError as exc:
            if exc.code == 40903:
                return False
            raise
        return bool(data["aborted"])

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}/snapshot")

    async def wait_until_subscribed(
        self, session_id: str, *, timeout: float = 15.0
    ) -> None:
        """Wait until ``subscribe_events`` has received its subscribe ack."""

        ready = self._subscription_ready.setdefault(session_id, asyncio.Event())
        await asyncio.wait_for(ready.wait(), timeout)

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

        await self._request(
            "GET",
            f"/sessions/{session_id}/status",
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
    return urlunsplit((scheme, parsed.netloc, "/api/v1/ws", "", ""))


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


def _cursor_from_mapping(value: Any) -> _EventCursor:
    if not isinstance(value, dict):
        raise KimiServerProtocolError("subscription cursor must be an object")
    return _EventCursor(seq=int(value["seq"]), epoch=value.get("epoch"))


def _advance_cursor(
    cursor: _EventCursor | None,
    frame: dict[str, Any],
    *,
    allow_sequence_gaps: bool = False,
) -> Literal["accept", "duplicate", "resync"]:
    seq = frame.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise KimiServerProtocolError("session event has an invalid seq")
    if cursor is None:
        return "accept"

    epoch = frame.get("epoch")
    if epoch is not None and cursor.epoch is not None and epoch != cursor.epoch:
        return "resync"
    if seq <= cursor.seq:
        return "duplicate"
    if not allow_sequence_gaps and seq != cursor.seq + 1:
        return "resync"
    return "accept"
