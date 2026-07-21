"""Route IM conversations to kimi sessions and render streamed replies."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .kimi_server import KimiServerAPIError, KimiServerClient, KimiServerError
from .platforms.base import CardAction, InboundFile, InboundMessage, PlatformAdapter
from .state import (
    PERMISSION_MODES,
    BridgeState,
    ConversationBinding,
    StateStore,
)


LOGGER = logging.getLogger(__name__)
DEFAULT_PERMISSION_MODE = "manual"
SESSION_TITLE_LIMIT = 80
SESSION_LIST_LIMIT = 10
INTERACTION_POLL_SECONDS = 1.0
INTERACTION_SUMMARY_LIMIT = 1200
TERMINAL_INTERACTION_ERROR_CODES = {40001, 40401, 40404, 40902}
STALE_INTERACTION_TEXT = (
    "This interaction is stale or was already resolved. Run the task again "
    "if you still need it."
)
PERMISSION_MODE_DESCRIPTIONS = {
    "manual": "Approvals and questions can be answered in chat.",
    "auto": "Fully autonomous; the agent never asks questions.",
    "yolo": "Regular tools are auto-approved; the agent may still ask questions.",
}

HELP_TEXT = """Commands:
/new [cwd] — create and bind a session
/sessions — list recent sessions
/switch <n|id> — bind a listed or explicit session
/mode <manual|auto|yolo> — manual uses chat interactions; auto never asks; yolo may ask questions
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
    conversation_id: str
    render: _RenderState = field(default_factory=_RenderState)
    task: asyncio.Task[None] | None = None
    interaction_task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _PendingInteraction:
    interaction_id: str
    kind: Literal["approval", "question"]
    request_id: str
    conversation_key: str
    session_id: str
    adapter: PlatformAdapter
    user_id: str
    conversation_id: str
    message_id: str
    request: dict[str, Any]
    timeout_task: asyncio.Task[None] | None = None


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
        interaction_timeout_seconds: float = 600.0,
        inbox_subdir: str = ".kimi-bridge-inbox",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        interaction_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        poll_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not model:
            raise ValueError("model must be non-empty")
        if edit_throttle_seconds <= 0:
            raise ValueError("edit_throttle_seconds must be positive")
        if interaction_timeout_seconds <= 0:
            raise ValueError("interaction_timeout_seconds must be positive")
        inbox_path = Path(inbox_subdir)
        if not inbox_subdir or inbox_path.is_absolute() or ".." in inbox_path.parts:
            raise ValueError("inbox_subdir must stay inside the session workspace")
        self._client = client
        self._state_store = state_store
        self._state: BridgeState = state_store.load()
        self._default_workspace = Path(default_workspace).expanduser().resolve()
        self._model = model
        self._edit_throttle_seconds = edit_throttle_seconds
        self._interaction_timeout_seconds = interaction_timeout_seconds
        self._inbox_subdir = inbox_subdir
        self._sleep = sleep
        self._interaction_sleep = interaction_sleep
        self._poll_sleep = poll_sleep
        self._clock = clock
        self._conversation_locks: dict[str, asyncio.Lock] = {}
        self._session_choices: dict[str, list[dict[str, Any]]] = {}
        self._active: _ActiveStream | None = None
        self._pending: dict[str, _PendingInteraction] = {}
        self._interaction_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._stop_active_stream()
        for pending in tuple(self._pending.values()):
            if pending.timeout_task is not None:
                pending.timeout_task.cancel()
        if self._pending:
            await asyncio.gather(
                *(
                    pending.timeout_task
                    for pending in self._pending.values()
                    if pending.timeout_task is not None
                ),
                return_exceptions=True,
            )
        self._pending.clear()

    async def handle_inbound(
        self, adapter: PlatformAdapter, msg: InboundMessage
    ) -> None:
        text = msg.text.strip()
        if not text and not msg.images and not msg.files:
            return
        conversation_key = _conversation_key(msg)
        lock = self._conversation_locks.setdefault(conversation_key, asyncio.Lock())
        async with lock:
            if text.startswith("/") and not msg.images and not msg.files:
                await self._handle_command(
                    conversation_key,
                    adapter,
                    msg.user_id,
                    msg.conversation_id,
                    text,
                )
                return

            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                self._default_workspace.mkdir(parents=True, exist_ok=True)
                binding = await self._create_and_bind(
                    conversation_key,
                    self._default_workspace,
                    _title_from_message(msg),
                )
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                msg.user_id,
                msg.conversation_id,
            )
            content = await self._build_prompt_content(binding, msg)
            result = await self._client.submit_prompt(
                binding.session_id,
                content,
                model=self._model,
                permission_mode=binding.permission_mode,
            )
            if result.get("status") in {"queued", "blocked"}:
                prompt_id = str(result["prompt_id"])
                try:
                    await self._client.steer_prompts(binding.session_id, [prompt_id])
                except KimiServerAPIError as exc:
                    if exc.code != 40001:
                        raise

    async def handle_card_action(
        self, adapter: PlatformAdapter, action: CardAction
    ) -> None:
        """Resolve one normalized platform card callback."""

        async with self._interaction_lock:
            pending = next(
                (
                    item
                    for item in self._pending.values()
                    if item.message_id == action.message_id
                ),
                None,
            )
            if pending is None:
                try:
                    await adapter.edit_card(
                        action.message_id,
                        _status_card(
                            "Interaction expired",
                            STALE_INTERACTION_TEXT,
                            template="grey",
                        ),
                    )
                finally:
                    await adapter.send_text(action.user_id, STALE_INTERACTION_TEXT)
                return
            if (
                _conversation_key(action) != pending.conversation_key
                or action.user_id != pending.user_id
                or action.conversation_id != pending.conversation_id
            ):
                await adapter.send_text(
                    action.user_id,
                    "This interaction belongs to another conversation.",
                )
                return
            action_interaction_id = action.value.get("interaction_id")
            if (
                action_interaction_id is not None
                and action_interaction_id != pending.interaction_id
            ):
                await adapter.send_text(action.user_id, STALE_INTERACTION_TEXT)
                return

            try:
                if pending.kind == "approval":
                    outcome = await self._resolve_approval_action(pending, action)
                else:
                    outcome = await self._resolve_question_action(pending, action)
                    if outcome is None:
                        await adapter.send_text(
                            action.user_id,
                            "Choose an option or enter a free-text answer.",
                        )
                        return
            except KimiServerAPIError as exc:
                if exc.code not in TERMINAL_INTERACTION_ERROR_CODES:
                    raise
                outcome = "Already resolved or expired"

            await self._clear_pending(pending)
            await adapter.edit_card(
                pending.message_id,
                _status_card(
                    "Interaction complete",
                    outcome,
                    template="green",
                ),
            )

    async def dispatch_event(self, session_key: str, event: dict[str, Any]) -> None:
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
        conversation_id: str,
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
                conversation_key,
                binding.session_id,
                adapter,
                user_id,
                conversation_id,
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
            await self._send_chunked(adapter, user_id, _format_sessions(sessions))
            return
        if command == "/switch":
            if not argument:
                await self._send_chunked(adapter, user_id, "Usage: /switch <n|id>")
                return
            session = await self._resolve_session(conversation_key, argument)
            if session is None:
                await self._send_chunked(
                    adapter, user_id, f"Session not found: {argument}"
                )
                return
            binding = self._binding_from_session(session)
            try:
                await self._ensure_active_stream(
                    conversation_key,
                    binding.session_id,
                    adapter,
                    user_id,
                    conversation_id,
                )
            except KimiServerError as exc:
                await self._send_chunked(
                    adapter,
                    user_id,
                    f"Could not switch to {binding.session_id}: {exc}",
                )
                return
            self._state.bindings[conversation_key] = binding
            self._state_store.save(self._state)
            await self._send_chunked(
                adapter, user_id, f"Switched to {binding.session_id}"
            )
            return
        if command == "/mode":
            if argument not in PERMISSION_MODES:
                await self._send_chunked(
                    adapter,
                    user_id,
                    "Usage: /mode <manual|auto|yolo>",
                )
                return
            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                await self._send_chunked(adapter, user_id, "No bound session.")
                return
            await self._client.update_profile(
                binding.session_id, permission_mode=argument
            )
            updated = ConversationBinding(
                session_id=binding.session_id,
                workspace=binding.workspace,
                permission_mode=argument,
            )
            self._state.bindings[conversation_key] = updated
            self._state_store.save(self._state)
            await self._send_chunked(
                adapter,
                user_id,
                f"Permission mode: {argument}\n{PERMISSION_MODE_DESCRIPTIONS[argument]}",
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
            self._client.list_sessions(busy=False, page_size=SESSION_LIST_LIMIT),
            self._client.list_sessions(busy=True, page_size=SESSION_LIST_LIMIT),
        )
        by_id = {str(session["id"]): session for session in [*idle, *busy]}
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
            permission_mode=DEFAULT_PERMISSION_MODE,
        )
        binding = ConversationBinding(
            session_id=session_id,
            workspace=str(workspace),
            permission_mode=DEFAULT_PERMISSION_MODE,
        )
        self._state.bindings[conversation_key] = binding
        self._state_store.save(self._state)
        return binding

    def _binding_from_session(self, session: dict[str, Any]) -> ConversationBinding:
        workspace = str(session["metadata"]["cwd"])
        agent_config = session.get("agent_config")
        mode = (
            agent_config.get("permission_mode")
            if isinstance(agent_config, dict)
            else None
        )
        if mode not in PERMISSION_MODES:
            mode = DEFAULT_PERMISSION_MODE
        binding = ConversationBinding(
            session_id=str(session["id"]),
            workspace=workspace,
            permission_mode=mode,
        )
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
        conversation_id: str,
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
            active.conversation_id = conversation_id
            return

        await self._stop_active_stream()
        active = _ActiveStream(
            conversation_key=conversation_key,
            session_id=session_id,
            adapter=adapter,
            user_id=user_id,
            conversation_id=conversation_id,
        )
        active.task = asyncio.create_task(
            self._consume_events(active),
            name=f"kimi-events-{session_id}",
        )
        stream_task = active.task
        self._active = active
        subscription_wait = asyncio.create_task(
            self._client.wait_until_subscribed(session_id),
            name=f"kimi-subscription-ready-{session_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                {stream_task, subscription_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stream_task in done:
                active.task = None
                stream_task.result()
                raise KimiServerError("kimi event stream ended before subscribing")
            await subscription_wait
        except BaseException:
            if not subscription_wait.done():
                subscription_wait.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await subscription_wait
            await self._stop_active_stream()
            raise
        stream_task.add_done_callback(self._stream_done)
        active.interaction_task = asyncio.create_task(
            self._poll_interactions(active),
            name=f"kimi-interactions-{session_id}",
        )
        active.interaction_task.add_done_callback(self._interaction_poll_done)

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
        if active.interaction_task is not None:
            active.interaction_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await active.interaction_task
        if active.task is not None:
            stream_task = active.task
            active.task = None
            stream_task.cancel()
            await asyncio.gather(stream_task, return_exceptions=True)

    async def _consume_events(self, active: _ActiveStream) -> None:
        async for event in self._client.subscribe_events(active.session_id):
            if self._active is not active:
                return
            await self.dispatch_event(active.conversation_key, event)

    def _stream_done(self, task: asyncio.Task[None]) -> None:
        active = self._active
        if active is not None and active.task is task:
            active.task = None
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi event stream stopped unexpectedly")

    def _interaction_poll_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            task.result()
        except Exception:
            LOGGER.exception("kimi interaction polling stopped unexpectedly")

    async def _poll_interactions(self, active: _ActiveStream) -> None:
        while self._active is active:
            try:
                await self._discover_interaction(active)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("failed to poll kimi interactions; retrying")
            await self._poll_sleep(INTERACTION_POLL_SECONDS)

    async def _discover_interaction(self, active: _ActiveStream) -> None:
        async with self._interaction_lock:
            if self._active is not active:
                return
            if active.conversation_key in self._pending:
                return
            if any(
                pending.session_id == active.session_id
                for pending in self._pending.values()
            ):
                return
            approvals, questions = await asyncio.gather(
                self._client.list_approvals(active.session_id),
                self._client.list_questions(active.session_id),
            )
            if approvals:
                kind: Literal["approval", "question"] = "approval"
                request = approvals[0]
                request_id = str(request["approval_id"])
            elif questions:
                kind = "question"
                request = questions[0]
                request_id = str(request["question_id"])
            else:
                return

            interaction_id = uuid.uuid4().hex
            session = await self._client.get_session(active.session_id)
            if kind == "approval":
                card = _approval_card(interaction_id, request, session)
            else:
                card = _question_card(interaction_id, request, session)
            message_id = await active.adapter.send_card(active.user_id, card)
            pending = _PendingInteraction(
                interaction_id=interaction_id,
                kind=kind,
                request_id=request_id,
                conversation_key=active.conversation_key,
                session_id=active.session_id,
                adapter=active.adapter,
                user_id=active.user_id,
                conversation_id=active.conversation_id,
                message_id=message_id,
                request=request,
            )
            self._pending[active.conversation_key] = pending
            pending.timeout_task = asyncio.create_task(
                self._expire_interaction(pending),
                name=f"interaction-timeout-{request_id}",
            )

    async def _expire_interaction(self, pending: _PendingInteraction) -> None:
        await self._interaction_sleep(self._interaction_timeout_seconds)
        async with self._interaction_lock:
            if self._pending.get(pending.conversation_key) is not pending:
                return
            try:
                if pending.kind == "approval":
                    await self._client.resolve_approval(
                        pending.session_id,
                        pending.request_id,
                        "rejected",
                    )
                    detail = "Timed out and was automatically rejected."
                else:
                    await self._client.dismiss_question(
                        pending.session_id, pending.request_id
                    )
                    detail = "Timed out and was automatically dismissed."
            except KimiServerAPIError as exc:
                if exc.code not in TERMINAL_INTERACTION_ERROR_CODES:
                    raise
                detail = "Expired after it had already been resolved."
            await self._clear_pending(pending)
            try:
                await pending.adapter.edit_card(
                    pending.message_id,
                    _status_card("Interaction timed out", detail, template="red"),
                )
            finally:
                await pending.adapter.send_text(pending.user_id, detail)

    async def _clear_pending(self, pending: _PendingInteraction) -> None:
        if self._pending.get(pending.conversation_key) is pending:
            self._pending.pop(pending.conversation_key)
        task = pending.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _resolve_approval_action(
        self, pending: _PendingInteraction, action: CardAction
    ) -> str:
        decision = action.value.get("decision")
        if decision not in {"approved", "rejected", "cancelled"}:
            raise ValueError("approval card callback has an invalid decision")
        await self._client.resolve_approval(
            pending.session_id,
            pending.request_id,
            decision,
        )
        return {
            "approved": "Approved",
            "rejected": "Rejected",
            "cancelled": "Cancelled",
        }[decision]

    async def _resolve_question_action(
        self, pending: _PendingInteraction, action: CardAction
    ) -> str | None:
        answers = _answers_from_action(pending, action)
        if answers is None:
            return None
        await self._client.resolve_question(
            pending.session_id,
            pending.request_id,
            answers,
        )
        return "Answer submitted"

    async def _build_prompt_content(
        self, binding: ConversationBinding, msg: InboundMessage
    ) -> list[dict[str, Any]]:
        text_parts: list[str] = []
        if msg.text.strip():
            text_parts.append(msg.text.strip())
        if msg.files:
            saved_paths = _save_inbound_files(
                Path(binding.workspace),
                self._inbox_subdir,
                msg.files,
            )
            text_parts.extend(f"Attached file saved at: {path}" for path in saved_paths)

        content: list[dict[str, Any]] = []
        if text_parts:
            content.append({"type": "text", "text": "\n\n".join(text_parts)})
        content.extend(
            {
                "type": "image",
                "source": {
                    "kind": "base64",
                    "media_type": image.media_type,
                    "data": base64.b64encode(image.data).decode("ascii"),
                },
            }
            for image in msg.images
        )
        return content

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
                snapshot_text = await self._snapshot_assistant_text(active.session_id)
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

    async def _flush_after(self, active: _ActiveStream, delay: float) -> None:
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
                    message_id = await active.adapter.send_text(active.user_id, chunk)
                    render.message_ids.append(message_id)
                elif (
                    index >= len(render.rendered_chunks)
                    or render.rendered_chunks[index] != chunk
                ):
                    await active.adapter.edit_text(render.message_ids[index], chunk)
            render.rendered_chunks = chunks
            render.last_flush = self._clock()

    async def _snapshot_assistant_text(self, session_id: str) -> str | None:
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


def _conversation_key(message: InboundMessage | CardAction) -> str:
    return f"{message.platform}:{message.bot_id}:{message.user_id}"


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


def _save_inbound_files(
    workspace: Path,
    inbox_subdir: str,
    files: tuple[InboundFile, ...],
) -> list[Path]:
    inbox = workspace / inbox_subdir
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    for inbound in files:
        filename = Path(inbound.name).name.strip()
        if filename in {"", ".", ".."}:
            filename = "attachment"
        stem = Path(filename).stem or "attachment"
        suffix = Path(filename).suffix
        candidate = inbox / filename
        index = 1
        while candidate.exists():
            candidate = inbox / f"{stem}-{index}{suffix}"
            index += 1
        candidate.write_bytes(inbound.data)
        saved.append(candidate.resolve())
    return saved


def _card_shell(
    title: str,
    subtitle: str,
    *,
    template: str,
    icon_token: str,
    tag_text: str,
    tag_color: str,
    elements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema": "2.0",
        "config": {"update_multi": True, "width_mode": "default"},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "subtitle": {"tag": "plain_text", "content": subtitle},
            "template": template,
            "icon": {"tag": "standard_icon", "token": icon_token},
            "text_tag_list": [
                {
                    "tag": "text_tag",
                    "text": {"tag": "plain_text", "content": tag_text},
                    "color": tag_color,
                }
            ],
        },
        "body": {
            "direction": "vertical",
            "padding": "12px 12px 20px 12px",
            "vertical_spacing": "12px",
            "elements": elements,
        },
    }


def _context_block(*lines: str) -> dict[str, Any]:
    return {
        "tag": "column_set",
        "flex_mode": "none",
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 1,
                "background_style": "grey-50",
                "padding": "12px",
                "vertical_spacing": "4px",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": line,
                            "lines": 8,
                        },
                    }
                    for line in lines
                    if line
                ],
            }
        ],
    }


def _approval_card(
    interaction_id: str,
    approval: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    tool_name = str(approval["tool_name"])
    action = str(approval.get("action") or "Approval required")
    display = approval.get("tool_input_display")
    summary = _summarize(display) if display is not None else ""
    workspace = str(session["metadata"]["cwd"])
    session_title = str(session.get("title") or "Untitled")
    buttons = [
        ("Approve", "approved", "primary_filled"),
        ("Reject", "rejected", "danger"),
        ("Cancel", "cancelled", "default"),
    ]
    button_block = {
        "tag": "column_set",
        "flex_mode": "trisect",
        "horizontal_spacing": "8px",
        "columns": [
            {
                "tag": "column",
                "elements": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": label},
                        "type": button_type,
                        "width": "fill",
                        "behaviors": [
                            {
                                "type": "callback",
                                "value": {
                                    "interaction_id": interaction_id,
                                    "decision": decision,
                                },
                            }
                        ],
                    }
                ],
            }
            for label, decision, button_type in buttons
        ],
    }
    return _card_shell(
        "Approval required",
        session_title,
        template="default",
        icon_token="approve_colorful",
        tag_text="Pending",
        tag_color="yellow",
        elements=[
            _context_block(
                f"Tool: {tool_name}",
                f"Action: {action}",
                f"Workspace: {workspace}",
                summary,
            ),
            button_block,
        ],
    )


def _question_card(
    interaction_id: str,
    request: dict[str, Any],
    session: dict[str, Any],
) -> dict[str, Any]:
    questions = request["questions"]
    session_title = str(session.get("title") or "Untitled")
    workspace = str(session["metadata"]["cwd"])
    elements: list[dict[str, Any]] = [
        _context_block(
            f"Session: {session_title}",
            f"Workspace: {workspace}",
        )
    ]
    if len(questions) == 1 and not questions[0].get("multi_select", False):
        question = questions[0]
        elements.append(_context_block(_question_description(question)))
        elements.append(
            {
                "tag": "column_set",
                "flex_mode": "flow",
                "horizontal_spacing": "8px",
                "columns": [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": str(option["label"])[:100],
                                },
                                "type": "default",
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "interaction_id": interaction_id,
                                            "question_id": question["id"],
                                            "option_id": option["id"],
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                    for option in question["options"]
                ]
                + [
                    {
                        "tag": "column",
                        "width": "auto",
                        "elements": [
                            {
                                "tag": "button",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "Skip",
                                },
                                "type": "default",
                                "behaviors": [
                                    {
                                        "type": "callback",
                                        "value": {
                                            "interaction_id": interaction_id,
                                            "question_id": question["id"],
                                            "skipped": True,
                                        },
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        if question.get("allow_other", False):
            elements.append(
                {
                    "tag": "form",
                    "name": "other_answer",
                    "direction": "vertical",
                    "vertical_spacing": "8px",
                    "elements": [
                        {
                            "tag": "input",
                            "name": "other_0",
                            "input_type": "multiline_text",
                            "rows": 3,
                            "max_length": 1000,
                            "width": "fill",
                            "label": {
                                "tag": "plain_text",
                                "content": str(
                                    question.get("other_label") or "Other answer"
                                ),
                            },
                        },
                        {
                            "tag": "button",
                            "name": "submit_other",
                            "form_action_type": "submit",
                            "text": {
                                "tag": "plain_text",
                                "content": "Submit answer",
                            },
                            "type": "primary_filled",
                            "width": "fill",
                        },
                    ],
                }
            )
    else:
        elements.append(_question_form(questions))
    return _card_shell(
        "Question from Kimi",
        session_title,
        template="blue",
        icon_token="myai_colorful",
        tag_text="Answer needed",
        tag_color="blue",
        elements=elements,
    )


def _question_form(questions: list[dict[str, Any]]) -> dict[str, Any]:
    form_elements: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        form_elements.append(
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": _question_description(question),
                    "text_size": "normal",
                    "lines": 8,
                },
            }
        )
        selector = {
            "tag": (
                "multi_select_static"
                if question.get("multi_select", False)
                else "select_static"
            ),
            "name": f"q_{index}",
            "required": False,
            "width": "fill",
            "placeholder": {
                "tag": "plain_text",
                "content": "Choose one or more options",
            },
            "options": [
                {
                    "text": {
                        "tag": "plain_text",
                        "content": str(option["label"]),
                    },
                    "value": str(option["id"]),
                }
                for option in question["options"]
            ],
        }
        form_elements.append(selector)
        if question.get("allow_other", False):
            form_elements.append(
                {
                    "tag": "input",
                    "name": f"other_{index}",
                    "input_type": "multiline_text",
                    "rows": 2,
                    "max_length": 1000,
                    "width": "fill",
                    "label": {
                        "tag": "plain_text",
                        "content": str(question.get("other_label") or "Other answer"),
                    },
                }
            )
    form_elements.append(
        {
            "tag": "button",
            "name": "submit_answers",
            "form_action_type": "submit",
            "text": {"tag": "plain_text", "content": "Submit answers"},
            "type": "primary_filled",
            "width": "fill",
        }
    )
    return {
        "tag": "form",
        "name": "question_answers",
        "direction": "vertical",
        "vertical_spacing": "8px",
        "elements": form_elements,
    }


def _answers_from_action(
    pending: _PendingInteraction, action: CardAction
) -> dict[str, dict[str, Any]] | None:
    questions = pending.request["questions"]
    question_id = action.value.get("question_id")
    if action.value.get("skipped") is True and isinstance(question_id, str):
        if not any(item["id"] == question_id for item in questions):
            raise ValueError("question card callback has an invalid question")
        return {question_id: {"kind": "skipped"}}

    option_id = action.value.get("option_id")
    if isinstance(option_id, str) and isinstance(question_id, str):
        question = next((item for item in questions if item["id"] == question_id), None)
        if question is None or option_id not in {
            option["id"] for option in question["options"]
        }:
            raise ValueError("question card callback has an invalid option")
        return {question_id: {"kind": "single", "option_id": option_id}}

    if not action.form_value and action.action_name != "submit_answers":
        return None
    answers: dict[str, dict[str, Any]] = {}
    for index, question in enumerate(questions):
        selected = _selected_values(action.form_value.get(f"q_{index}"))
        option_ids = {str(option["id"]) for option in question["options"]}
        if any(option_id not in option_ids for option_id in selected):
            raise ValueError("question card callback has an invalid option")
        if not question.get("multi_select", False) and len(selected) > 1:
            raise ValueError("single-select question received multiple options")
        other_value = action.form_value.get(f"other_{index}")
        other = other_value.strip() if isinstance(other_value, str) else ""
        if other and not question.get("allow_other", False):
            raise ValueError("question does not allow a free-text answer")
        if question.get("multi_select", False):
            if other:
                answers[str(question["id"])] = {
                    "kind": "multi_with_other",
                    "option_ids": selected,
                    "other_text": other,
                }
            elif selected:
                answers[str(question["id"])] = {
                    "kind": "multi",
                    "option_ids": selected,
                }
            else:
                answers[str(question["id"])] = {"kind": "skipped"}
        elif other:
            answers[str(question["id"])] = {
                "kind": "other",
                "text": other,
            }
        elif selected:
            answers[str(question["id"])] = {
                "kind": "single",
                "option_id": selected[0],
            }
        else:
            answers[str(question["id"])] = {"kind": "skipped"}
    return answers


def _selected_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _question_description(question: dict[str, Any]) -> str:
    lines: list[str] = []
    header = question.get("header")
    if isinstance(header, str) and header:
        lines.append(header)
    question_text = str(question["question"])
    if not lines or lines[-1] != question_text:
        lines.append(question_text)
    body = question.get("body")
    if isinstance(body, str) and body:
        lines.append(body)
    for option in question["options"]:
        description = option.get("description")
        if isinstance(description, str) and description:
            lines.append(f"{option['label']}: {description}")
    return "\n".join(lines)


def _summarize(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    if len(text) > INTERACTION_SUMMARY_LIMIT:
        return text[: INTERACTION_SUMMARY_LIMIT - 1] + "…"
    return text


def _status_card(title: str, detail: str, *, template: str) -> dict[str, Any]:
    tag_color = {
        "green": "green",
        "red": "red",
        "grey": "neutral",
    }.get(template, "blue")
    return _card_shell(
        title,
        "Kimi bridge",
        template=template,
        icon_token="notice_colorful",
        tag_text=title,
        tag_color=tag_color,
        elements=[
            _context_block("Interaction status"),
            _context_block(detail),
        ],
    )
