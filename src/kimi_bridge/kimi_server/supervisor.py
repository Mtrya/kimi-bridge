"""Foreground supervision for the managed local Kimi server."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from ..compatibility import (
    KimiExecutableIdentity,
    KimiProduct,
    KimiProductFingerprintError,
    VersionSupport,
    identify_kimi_executable,
    legacy_product_message,
    unknown_version_warning,
)
from .contract import KIMI_REQUIRED_WEB_FLAGS
from .types import (
    KimiServerAuthenticationError,
    KimiServerStartupError,
    ServerConnection,
)


LOGGER = logging.getLogger(__name__)
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
        process_env: Mapping[str, str] | None = None,
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
        self._process_env = dict(process_env) if process_env is not None else None

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

        web_help_output = await self._run_cli_probe("web", "--help")
        missing_flags = sorted(
            flag
            for flag in KIMI_REQUIRED_WEB_FLAGS
            if flag not in web_help_output
        )
        if missing_flags:
            raise KimiServerStartupError(
                "kimi web is missing required bridge flags: "
                + ", ".join(missing_flags)
            )

        self._executable_identity = identity
        if identity.support is VersionSupport.SUPPORTED:
            LOGGER.info("kimi-code executable version: %s", identity.version)
        else:
            LOGGER.warning("%s", unknown_version_warning(identity.version))

    async def _run_cli_probe(self, *arguments: str) -> str:
        command_text = " ".join(("kimi", *arguments))
        try:
            kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.STDOUT,
            }
            if self._process_env is not None:
                kwargs["env"] = self._process_env
            process = await self._process_factory(
                self._executable,
                *arguments,
                **kwargs,
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
            kwargs: dict[str, Any] = {
                "stdout": asyncio.subprocess.PIPE,
                "stderr": asyncio.subprocess.STDOUT,
                "start_new_session": True,
            }
            if self._process_env is not None:
                kwargs["env"] = self._process_env
            process = await self._process_factory(*command, **kwargs)
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
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), self._shutdown_timeout)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
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
