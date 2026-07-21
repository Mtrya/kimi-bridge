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
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


LOGGER = logging.getLogger(__name__)
EXPECTED_SERVER_VERSION = "0.28.1"

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
    """The WebSocket peer violated or rejected the expected protocol."""


@dataclass(frozen=True, slots=True)
class ServerConnection:
    """Current endpoint for one generation of the managed child."""

    base_url: str
    port: int
    generation: int
    token: str = field(repr=False)


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

    async def __aenter__(self) -> KimiServerSupervisor:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.stop()

    async def start(self) -> ServerConnection:
        """Start supervision and wait for the first bearer token."""

        if self._task is not None:
            return await self.wait_until_ready()

        await self._check_executable_version()
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

    async def _check_executable_version(self) -> None:
        try:
            process = await self._process_factory(
                self._executable,
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except FileNotFoundError as exc:
            raise KimiServerStartupError(
                "kimi-code is not installed or 'kimi' is not on PATH"
            ) from exc

        try:
            if process.stdout is None:
                raise KimiServerStartupError("kimi --version stdout was not captured")
            raw_output = await asyncio.wait_for(
                process.stdout.read(), self._startup_timeout
            )
            returncode = await asyncio.wait_for(
                process.wait(), self._startup_timeout
            )
        except TimeoutError as exc:
            await self._terminate_process(process)
            raise KimiServerStartupError(
                f"kimi --version did not finish within {self._startup_timeout:g}s"
            ) from exc
        except BaseException:
            await self._terminate_process(process)
            raise

        output = _redact_tokens(raw_output.decode("utf-8", errors="replace")).strip()
        if returncode != 0:
            raise KimiServerStartupError(
                f"kimi --version exited with status {returncode}: "
                f"{output or 'no output'}"
            )
        if output != EXPECTED_SERVER_VERSION:
            raise KimiServerStartupError(
                "unsupported kimi-code executable version: "
                f"expected {EXPECTED_SERVER_VERSION}, got {output or 'no output'}"
            )
        LOGGER.info("kimi-code executable version: %s", output)

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

    async def check_server_version(self) -> str:
        metadata = await self.meta()
        server_version = str(metadata["server_version"])
        LOGGER.info("kimi server version: %s", server_version)
        if server_version != EXPECTED_SERVER_VERSION:
            raise KimiServerStartupError(
                "unsupported kimi server version: "
                f"expected {EXPECTED_SERVER_VERSION}, got {server_version}"
            )
        return server_version

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
        **profile: Any,
    ) -> dict[str, Any]:
        content_items = (
            [{"type": "text", "text": content}] if isinstance(content, str) else content
        )
        payload: dict[str, Any] = {"content": content_items}
        payload.update(profile)
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
        self, session_id: str, **agent_config: Any
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/profile",
            json_body={"agent_config": agent_config},
        )

    async def list_approvals(self, session_id: str) -> list[dict[str, Any]]:
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
        return list(data["items"])

    async def resolve_approval(
        self,
        session_id: str,
        approval_id: str,
        decision: Literal["approved", "rejected", "cancelled"],
    ) -> bool:
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/approvals/{approval_id}",
            json_body={"decision": decision},
        )
        return bool(data["resolved"])

    async def list_questions(self, session_id: str) -> list[dict[str, Any]]:
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
        return list(data["items"])

    async def resolve_question(
        self,
        session_id: str,
        question_id: str,
        answers: dict[str, dict[str, Any]],
    ) -> bool:
        data = await self._request(
            "POST",
            f"/sessions/{session_id}/questions/{question_id}",
            json_body={"answers": answers, "method": "click"},
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
                                yield frame
                                continue

                            disposition = _advance_cursor(cursor, frame)
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
        payload: dict[str, Any] = {"session_ids": [session_id]}
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


def _cursor_from_mapping(value: Any) -> _EventCursor:
    if not isinstance(value, dict):
        raise KimiServerProtocolError("subscription cursor must be an object")
    return _EventCursor(seq=int(value["seq"]), epoch=value.get("epoch"))


def _advance_cursor(
    cursor: _EventCursor | None, frame: dict[str, Any]
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
    if seq != cursor.seq + 1:
        return "resync"
    return "accept"
