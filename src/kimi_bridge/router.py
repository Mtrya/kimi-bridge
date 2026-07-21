"""Route IM conversations to kimi sessions and render streamed replies."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .kimi_server import KimiServerAPIError, KimiServerClient
from .platforms.base import InboundMessage, PlatformAdapter
from .state import BridgeState, ConversationBinding, StateStore


LOGGER = logging.getLogger(__name__)
PERMISSION_MODE = "auto"
SESSION_TITLE_LIMIT = 80
SESSION_LIST_LIMIT = 10

HELP_TEXT = """Commands:
/new [cwd] — create and bind a session
/sessions — list recent sessions
/switch <n|id> — bind a listed or explicit session
/stop — abort the active prompt
/help — show this help"""


@dataclass(slots=True)
class _RenderState:
    text: str = ""
    message_ids: list[str] = field(default_factory=list)
    rendered_chunks: list[str] = field(default_factory=list)
    turn_active: bool = False
    last_flush: float | None = None
    delayed_flush: asyncio.Task[None] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class _ActiveStream:
    conversation_key: str
    session_id: str
    adapter: PlatformAdapter
    user_id: str
    render: _RenderState = field(default_factory=_RenderState)
    task: asyncio.Task[None] | None = None


class ChatRouter:
    """Own conversation bindings, bridge commands, and stream rendering."""

    def __init__(
        self,
        client: KimiServerClient,
        *,
        state_store: StateStore,
        default_workspace: str | Path,
        model: str,
        edit_throttle_seconds: float = 1.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("model must be non-empty")
        if edit_throttle_seconds <= 0:
            raise ValueError("edit_throttle_seconds must be positive")
        self._client = client
        self._state_store = state_store
        self._state: BridgeState = state_store.load()
        self._default_workspace = Path(default_workspace).expanduser().resolve()
        self._model = model
        self._edit_throttle_seconds = edit_throttle_seconds
        self._sleep = sleep
        self._clock = clock
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._session_choices: dict[str, list[dict[str, Any]]] = {}
        self._active: _ActiveStream | None = None

    async def close(self) -> None:
        await self._stop_active_stream()

    async def handle_inbound(
        self, adapter: PlatformAdapter, msg: InboundMessage
    ) -> None:
        text = msg.text.strip()
        if not text:
            return
        conversation_key = _conversation_key(msg)
        lock = self._conversation_locks.setdefault(
            conversation_key, asyncio.Lock()
        )
        async with lock:
            if text.startswith("/"):
                await self._handle_command(
                    conversation_key, adapter, msg.user_id, text
                )
                return

            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                self._default_workspace.mkdir(parents=True, exist_ok=True)
                binding = await self._create_and_bind(
                    conversation_key,
                    self._default_workspace,
                    _title_from_text(text),
                )
            await self._ensure_active_stream(
                conversation_key, binding.session_id, adapter, msg.user_id
            )
            await self._client.submit_prompt(
                binding.session_id,
                text,
                model=self._model,
                permission_mode=PERMISSION_MODE,
            )

    async def dispatch_event(
        self, session_key: str, event: dict[str, Any]
    ) -> None:
        """Translate one raw session event into throttled platform output."""

        active = self._active
        if active is None or active.conversation_key != session_key:
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        event_type = payload.get("type") or event.get("type")

        if event_type == "resync_required":
            snapshot = event.get("snapshot")
            if isinstance(snapshot, dict):
                await self._render_resync_snapshot(active, snapshot)
            return
        if event_type == "turn.started":
            await self._reset_render(active)
            return
        if event_type == "assistant.delta":
            delta = payload.get("delta")
            if not isinstance(delta, str):
                return
            await self._apply_delta(active, event.get("offset"), delta)
            await self._maybe_flush(active)
            return
        if event_type == "turn.ended":
            snapshot_text = await self._snapshot_assistant_text(active.session_id)
            if snapshot_text is not None:
                active.render.text = snapshot_text
            await self._flush(active)
            active.render.turn_active = False

    async def _handle_command(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        user_id: str,
        text: str,
    ) -> None:
        command, _, argument = text.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command == "/help":
            await self._send_chunked(adapter, user_id, HELP_TEXT)
            return
        if command == "/new":
            try:
                workspace = await self._resolve_new_workspace(argument)
            except ValueError as exc:
                await self._send_chunked(adapter, user_id, str(exc))
                return
            binding = await self._create_and_bind(
                conversation_key,
                workspace,
                f"Kimi: {workspace.name or workspace}",
            )
            await self._ensure_active_stream(
                conversation_key, binding.session_id, adapter, user_id
            )
            await self._send_chunked(
                adapter,
                user_id,
                f"Created session {binding.session_id}\nWorkspace: {binding.workspace}",
            )
            return
        if command == "/sessions":
            sessions = await self._list_recent_sessions()
            self._session_choices[conversation_key] = sessions
            await self._send_chunked(
                adapter, user_id, _format_sessions(sessions)
            )
            return
        if command == "/switch":
            if not argument:
                await self._send_chunked(
                    adapter, user_id, "Usage: /switch <n|id>"
                )
                return
            session = await self._resolve_session(conversation_key, argument)
            if session is None:
                await self._send_chunked(
                    adapter, user_id, f"Session not found: {argument}"
                )
                return
            binding = self._bind_existing(conversation_key, session)
            await self._ensure_active_stream(
                conversation_key, binding.session_id, adapter, user_id
            )
            await self._send_chunked(
                adapter, user_id, f"Switched to {binding.session_id}"
            )
            return
        if command == "/stop":
            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                await self._send_chunked(adapter, user_id, "No bound session.")
                return
            aborted = await self._client.abort_prompt(binding.session_id)
            await self._send_chunked(
                adapter,
                user_id,
                "Stopped." if aborted else "No active prompt.",
            )
            return

        await self._send_chunked(
            adapter, user_id, f"Unknown command: {command}\nUse /help."
        )

    async def _list_recent_sessions(self) -> list[dict[str, Any]]:
        idle, busy = await asyncio.gather(
            self._client.list_sessions(
                busy=False, page_size=SESSION_LIST_LIMIT
            ),
            self._client.list_sessions(
                busy=True, page_size=SESSION_LIST_LIMIT
            ),
        )
        by_id = {
            str(session["id"]): session
            for session in [*idle, *busy]
        }
        sessions = list(by_id.values())
        sessions.sort(key=_session_recency_key, reverse=True)
        return sessions[:SESSION_LIST_LIMIT]

    async def _resolve_new_workspace(self, argument: str) -> Path:
        if not argument:
            self._default_workspace.mkdir(parents=True, exist_ok=True)
            return self._default_workspace
        workspace = Path(argument).expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"workspace is not a directory: {workspace}")
        return workspace

    async def _create_and_bind(
        self, conversation_key: str, workspace: Path, title: str
    ) -> ConversationBinding:
        session_id = await self._client.create_session(
            str(workspace),
            title=title,
            permission_mode=PERMISSION_MODE,
        )
        binding = ConversationBinding(
            session_id=session_id,
            workspace=str(workspace),
            permission_mode=PERMISSION_MODE,
        )
        self._state.bindings[conversation_key] = binding
        self._state_store.save(self._state)
        return binding

    def _bind_existing(
        self, conversation_key: str, session: dict[str, Any]
    ) -> ConversationBinding:
        workspace = str(session["metadata"]["cwd"])
        binding = ConversationBinding(
            session_id=str(session["id"]),
            workspace=workspace,
            permission_mode=PERMISSION_MODE,
        )
        self._state.bindings[conversation_key] = binding
        self._state_store.save(self._state)
        return binding

    async def _resolve_session(
        self, conversation_key: str, selector: str
    ) -> dict[str, Any] | None:
        if selector.isdecimal():
            index = int(selector) - 1
            choices = self._session_choices.get(conversation_key, [])
            if 0 <= index < len(choices):
                return choices[index]
            return None
        try:
            return await self._client.get_session(selector)
        except KimiServerAPIError as exc:
            if exc.code == 40401:
                return None
            raise

    async def _ensure_active_stream(
        self,
        conversation_key: str,
        session_id: str,
        adapter: PlatformAdapter,
        user_id: str,
    ) -> None:
        active = self._active
        if (
            active is not None
            and active.conversation_key == conversation_key
            and active.session_id == session_id
            and active.task is not None
            and not active.task.done()
        ):
            active.adapter = adapter
            active.user_id = user_id
            return

        await self._stop_active_stream()
        await self._client.resume_session(session_id)
        active = _ActiveStream(
            conversation_key=conversation_key,
            session_id=session_id,
            adapter=adapter,
            user_id=user_id,
        )
        active.task = asyncio.create_task(
            self._consume_events(active),
            name=f"kimi-events-{session_id}",
        )
        active.task.add_done_callback(self._stream_done)
        self._active = active
        try:
            await self._client.wait_until_subscribed(session_id)
        except BaseException:
            await self._stop_active_stream()
            raise

    async def _stop_active_stream(self) -> None:
        active = self._active
        self._active = None
        if active is None:
            return
        delayed = active.render.delayed_flush
        if delayed is not None:
            delayed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delayed
        if active.task is not None:
            active.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.task

    async def _consume_events(self, active: _ActiveStream) -> None:
        async for event in self._client.subscribe_events(active.session_id):
            if self._active is not active:
                return
            await self.dispatch_event(active.conversation_key, event)

    def _stream_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi event stream stopped unexpectedly")

    async def _reset_render(self, active: _ActiveStream) -> None:
        delayed = active.render.delayed_flush
        if delayed is not None:
            delayed.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await delayed
        active.render = _RenderState(turn_active=True)

    async def _apply_delta(
        self, active: _ActiveStream, offset: Any, delta: str
    ) -> None:
        render = active.render
        render.turn_active = True
        if isinstance(offset, int) and not isinstance(offset, bool):
            if render.text[offset : offset + len(delta)] == delta:
                return
            if offset != len(render.text):
                snapshot_text = await self._snapshot_assistant_text(
                    active.session_id
                )
                if snapshot_text is not None:
                    render.text = snapshot_text
                    if render.text[offset : offset + len(delta)] == delta:
                        return
            if offset <= len(render.text):
                render.text = render.text[:offset] + delta
                return
        render.text += delta

    async def _maybe_flush(self, active: _ActiveStream) -> None:
        render = active.render
        now = self._clock()
        if not render.message_ids or render.last_flush is None:
            await self._flush(active)
            return
        elapsed = now - render.last_flush
        if elapsed >= self._edit_throttle_seconds:
            await self._flush(active)
            return
        if render.delayed_flush is None or render.delayed_flush.done():
            render.delayed_flush = asyncio.create_task(
                self._flush_after(active, self._edit_throttle_seconds - elapsed),
                name="throttled-message-edit",
            )

    async def _flush_after(
        self, active: _ActiveStream, delay: float
    ) -> None:
        try:
            await self._sleep(delay)
            if self._active is active:
                await self._flush(active)
        finally:
            if active.render.delayed_flush is asyncio.current_task():
                active.render.delayed_flush = None

    async def _flush(self, active: _ActiveStream) -> None:
        render = active.render
        if not render.text:
            return
        async with render.lock:
            chunks = _chunk_text(render.text, active.adapter.message_limit)
            for index, chunk in enumerate(chunks):
                if index >= len(render.message_ids):
                    message_id = await active.adapter.send_text(
                        active.user_id, chunk
                    )
                    render.message_ids.append(message_id)
                elif (
                    index >= len(render.rendered_chunks)
                    or render.rendered_chunks[index] != chunk
                ):
                    await active.adapter.edit_text(
                        render.message_ids[index], chunk
                    )
            render.rendered_chunks = chunks
            render.last_flush = self._clock()

    async def _snapshot_assistant_text(
        self, session_id: str
    ) -> str | None:
        snapshot = await self._client.get_snapshot(session_id)
        return _assistant_text_from_snapshot(snapshot)

    async def _render_resync_snapshot(
        self, active: _ActiveStream, snapshot: dict[str, Any]
    ) -> None:
        in_flight = snapshot.get("in_flight_turn")
        if isinstance(in_flight, dict):
            text = in_flight.get("assistant_text")
            if not isinstance(text, str):
                return
            if not active.render.turn_active:
                await self._reset_render(active)
            active.render.text = text
            await self._flush(active)
            return

        if not active.render.turn_active:
            return
        text = _assistant_text_from_snapshot(snapshot)
        if text is not None:
            active.render.text = text
            await self._flush(active)
        active.render.turn_active = False

    async def _send_chunked(
        self, adapter: PlatformAdapter, user_id: str, text: str
    ) -> None:
        for chunk in _chunk_text(text, adapter.message_limit):
            await adapter.send_text(user_id, chunk)


def _conversation_key(message: InboundMessage) -> str:
    return f"{message.platform}:{message.bot_id}:{message.user_id}"


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
        lines.append(
            f"{index}. {title} [{status}]\n{workspace}\n{session['id']}"
        )
    return "\n\n".join(lines)


def _session_recency_key(session: dict[str, Any]) -> str:
    updated_at = session.get("updated_at")
    return str(updated_at) if updated_at is not None else ""


def _assistant_text_from_snapshot(snapshot: dict[str, Any]) -> str | None:
    in_flight = snapshot.get("in_flight_turn")
    if isinstance(in_flight, dict):
        text = in_flight.get("assistant_text")
        if isinstance(text, str):
            return text

    messages = snapshot.get("messages")
    items = messages.get("items", []) if isinstance(messages, dict) else []
    for message in reversed(items):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        content = message.get("content", [])
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        ]
        return "".join(str(part) for part in parts)
    return None
