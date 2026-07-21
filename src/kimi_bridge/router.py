"""Route IM conversations to kimi sessions and render streamed replies."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .interactions import (
    ApprovalPrompt,
    ApprovalRequest,
    ApprovalResponse,
    InteractionOutcome,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    QuestionAnswer,
    QuestionPrompt,
    QuestionRequest,
    QuestionResponse,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from .kimi_server import KimiServerAPIError, KimiServerClient, KimiServerError
from .platforms.base import (
    ActorRef,
    ConversationRef,
    InboundFile,
    InboundInteraction,
    InboundMessage,
    MessageRef,
    PlatformAdapter,
)
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
    messages: list[MessageRef] = field(default_factory=list)
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
    conversation: ConversationRef
    actor: ActorRef
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
    conversation: ConversationRef
    actor: ActorRef
    message: MessageRef
    request: ApprovalRequest | QuestionRequest
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
                    msg.conversation,
                    msg.actor,
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
                msg.conversation,
                msg.actor,
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

    async def handle_interaction(
        self, adapter: PlatformAdapter, action: InboundInteraction
    ) -> None:
        """Resolve one normalized platform interaction submission."""

        async with self._interaction_lock:
            pending = next(
                (
                    item
                    for item in self._pending.values()
                    if item.message == action.source
                ),
                None,
            )
            if pending is None:
                try:
                    await adapter.finish_interaction(
                        action.source,
                        InteractionOutcome(
                            state="stale",
                            detail=STALE_INTERACTION_TEXT,
                        ),
                    )
                finally:
                    await adapter.send_text(
                        action.conversation, STALE_INTERACTION_TEXT
                    )
                return
            if (
                _conversation_key(action) != pending.conversation_key
                or action.actor.id != pending.actor.id
                or action.conversation != pending.conversation
            ):
                await adapter.send_text(
                    action.conversation,
                    "This interaction belongs to another conversation.",
                )
                return
            action_interaction_id = action.interaction_id
            if (
                action_interaction_id is not None
                and action_interaction_id != pending.interaction_id
            ):
                await adapter.send_text(
                    action.conversation, STALE_INTERACTION_TEXT
                )
                return

            try:
                if pending.kind == "approval":
                    outcome = await self._resolve_approval_action(pending, action)
                else:
                    outcome = await self._resolve_question_action(pending, action)
                    if outcome is None:
                        await adapter.send_text(
                            action.conversation,
                            "Choose an option or enter a free-text answer.",
                        )
                        return
            except KimiServerAPIError as exc:
                if exc.code not in TERMINAL_INTERACTION_ERROR_CODES:
                    raise
                outcome = "Already resolved or expired"

            await self._clear_pending(pending)
            await adapter.finish_interaction(
                pending.message,
                InteractionOutcome(state="completed", detail=outcome),
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
        conversation: ConversationRef,
        actor: ActorRef,
        text: str,
    ) -> None:
        command, _, argument = text.partition(" ")
        command = command.lower()
        argument = argument.strip()

        if command == "/help":
            await self._send_chunked(adapter, conversation, HELP_TEXT)
            return
        if command == "/new":
            try:
                workspace = await self._resolve_new_workspace(argument)
            except ValueError as exc:
                await self._send_chunked(adapter, conversation, str(exc))
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
                conversation,
                actor,
            )
            await self._send_chunked(
                adapter,
                conversation,
                f"Created session {binding.session_id}\nWorkspace: {binding.workspace}",
            )
            return
        if command == "/sessions":
            sessions = await self._list_recent_sessions()
            self._session_choices[conversation_key] = sessions
            await self._send_chunked(
                adapter, conversation, _format_sessions(sessions)
            )
            return
        if command == "/switch":
            if not argument:
                await self._send_chunked(
                    adapter, conversation, "Usage: /switch <n|id>"
                )
                return
            session = await self._resolve_session(conversation_key, argument)
            if session is None:
                await self._send_chunked(
                    adapter, conversation, f"Session not found: {argument}"
                )
                return
            binding = self._binding_from_session(session)
            try:
                await self._ensure_active_stream(
                    conversation_key,
                    binding.session_id,
                    adapter,
                    conversation,
                    actor,
                )
            except KimiServerError as exc:
                await self._send_chunked(
                    adapter,
                    conversation,
                    f"Could not switch to {binding.session_id}: {exc}",
                )
                return
            self._state.bindings[conversation_key] = binding
            self._state_store.save(self._state)
            await self._send_chunked(
                adapter, conversation, f"Switched to {binding.session_id}"
            )
            return
        if command == "/mode":
            if argument not in PERMISSION_MODES:
                await self._send_chunked(
                    adapter,
                    conversation,
                    "Usage: /mode <manual|auto|yolo>",
                )
                return
            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                await self._send_chunked(
                    adapter, conversation, "No bound session."
                )
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
                conversation,
                f"Permission mode: {argument}\n{PERMISSION_MODE_DESCRIPTIONS[argument]}",
            )
            return
        if command == "/stop":
            binding = self._state.bindings.get(conversation_key)
            if binding is None:
                await self._send_chunked(
                    adapter, conversation, "No bound session."
                )
                return
            async with self._interaction_lock:
                pending = self._pending.get(conversation_key)
                session_ids: list[str] = []
                if pending is not None:
                    session_ids.append(pending.session_id)
                if binding.session_id not in session_ids:
                    session_ids.append(binding.session_id)
                aborted = False
                for session_id in session_ids:
                    aborted = await self._client.abort_prompt(session_id) or aborted
                if pending is not None:
                    await self._clear_pending(pending)
                    await pending.adapter.finish_interaction(
                        pending.message,
                        InteractionOutcome(
                            state="cancelled",
                            detail="Cancelled by /stop.",
                        ),
                    )
            result = "Stopped." if aborted or pending is not None else "No active prompt."
            await self._send_chunked(adapter, conversation, result)
            return

        await self._send_chunked(
            adapter, conversation, f"Unknown command: {command}\nUse /help."
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
        conversation: ConversationRef,
        actor: ActorRef,
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
            active.conversation = conversation
            active.actor = actor
            return

        await self._stop_active_stream()
        active = _ActiveStream(
            conversation_key=conversation_key,
            session_id=session_id,
            adapter=adapter,
            conversation=conversation,
            actor=actor,
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
                request_id = request.id
            elif questions:
                kind = "question"
                request = questions[0]
                request_id = request.id
            else:
                return

            interaction_id = uuid.uuid4().hex
            session = await self._client.get_session(active.session_id)
            if kind == "approval":
                assert isinstance(request, ApprovalRequest)
                prompt = ApprovalPrompt(
                    interaction_id=interaction_id,
                    request=request,
                    session_title=str(session.get("title") or "Untitled"),
                    workspace=str(session["metadata"]["cwd"]),
                )
            else:
                assert isinstance(request, QuestionRequest)
                prompt = QuestionPrompt(
                    interaction_id=interaction_id,
                    request=request,
                    session_title=str(session.get("title") or "Untitled"),
                    workspace=str(session["metadata"]["cwd"]),
                )
            message = await active.adapter.present_interaction(
                active.conversation, prompt
            )
            pending = _PendingInteraction(
                interaction_id=interaction_id,
                kind=kind,
                request_id=request_id,
                conversation_key=active.conversation_key,
                session_id=active.session_id,
                adapter=active.adapter,
                conversation=active.conversation,
                actor=active.actor,
                message=message,
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
                await pending.adapter.finish_interaction(
                    pending.message,
                    InteractionOutcome(state="timed_out", detail=detail),
                )
            finally:
                await pending.adapter.send_text(pending.conversation, detail)

    async def _clear_pending(self, pending: _PendingInteraction) -> None:
        if self._pending.get(pending.conversation_key) is pending:
            self._pending.pop(pending.conversation_key)
        task = pending.timeout_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _resolve_approval_action(
        self, pending: _PendingInteraction, action: InboundInteraction
    ) -> str:
        if not isinstance(pending.request, ApprovalRequest):
            raise TypeError("approval interaction has a question request")
        if not isinstance(action.response, ApprovalResponse):
            raise ValueError("approval interaction has an invalid response")
        decision = action.response.decision
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
        self, pending: _PendingInteraction, action: InboundInteraction
    ) -> str | None:
        if not isinstance(pending.request, QuestionRequest):
            raise TypeError("question interaction has an approval request")
        if action.response is None:
            return None
        if not isinstance(action.response, QuestionResponse):
            raise ValueError("question interaction has an invalid response")
        answers = _validate_question_answers(
            pending.request, action.response.answers
        )
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
        if not render.messages or render.last_flush is None:
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
                if index >= len(render.messages):
                    message = await active.adapter.send_text(
                        active.conversation, chunk
                    )
                    render.messages.append(message)
                elif (
                    index >= len(render.rendered_chunks)
                    or render.rendered_chunks[index] != chunk
                ):
                    await active.adapter.edit_text(render.messages[index], chunk)
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
        self, adapter: PlatformAdapter, conversation: ConversationRef, text: str
    ) -> None:
        for chunk in _chunk_text(text, adapter.message_limit):
            await adapter.send_text(conversation, chunk)


def _conversation_key(message: InboundMessage | InboundInteraction) -> str:
    conversation = message.conversation
    return f"{conversation.platform}:{conversation.bot_id}:{message.actor.id}"


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


def _validate_question_answers(
    request: QuestionRequest,
    answers: tuple[QuestionAnswer, ...],
) -> tuple[QuestionAnswer, ...]:
    questions = {question.id: question for question in request.questions}
    if len({answer.question_id for answer in answers}) != len(answers):
        raise ValueError("question response contains duplicate answers")
    if {answer.question_id for answer in answers} != set(questions):
        raise ValueError("question response must answer or skip every question")

    for answer in answers:
        question = questions[answer.question_id]
        option_ids = {option.id for option in question.options}
        if isinstance(answer, SkippedAnswer):
            continue
        if isinstance(answer, SingleChoiceAnswer):
            if question.multi_select or answer.option_id not in option_ids:
                raise ValueError("question response contains an invalid single choice")
            continue
        if isinstance(answer, MultipleChoiceAnswer):
            if (
                not question.multi_select
                or not answer.option_ids
                or any(option_id not in option_ids for option_id in answer.option_ids)
            ):
                raise ValueError("question response contains invalid multiple choices")
            continue
        if isinstance(answer, OtherAnswer):
            if question.multi_select or not question.allow_other or not answer.text:
                raise ValueError("question response contains invalid free text")
            continue
        if isinstance(answer, MultipleChoiceWithOtherAnswer):
            if (
                not question.multi_select
                or not question.allow_other
                or not answer.text
                or any(option_id not in option_ids for option_id in answer.option_ids)
            ):
                raise ValueError(
                    "question response contains invalid choices with free text"
                )
            continue
        raise TypeError(f"unsupported question answer: {type(answer).__name__}")
    return answers
