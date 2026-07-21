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
from typing import Any, Literal, cast

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
from .kimi_server import (
    GoalControl,
    GoalInfo,
    KimiServerAPIError,
    KimiServerClient,
    KimiServerError,
    KimiServerProtocolError,
    ModelInfo,
    SessionProfile,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    TaskStatus,
    ToolInfo,
)
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
TASK_OUTPUT_BYTES = 8 * 1024
INTERACTION_POLL_SECONDS = 1.0
TASK_STATUSES: frozenset[str] = frozenset(
    {"running", "completed", "failed", "cancelled"}
)
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
/model [alias] — show or set the exact session model
/effort [effort] — show or set thinking effort for the current model
/plan [on|off] — show or explicitly set plan mode
/status — show bound session and runtime state
/title [text] — show or rename the session
/usage — show live session token totals and context usage
/tasks [running|completed|failed|cancelled] — list tasks
/tasks show <id> — inspect a task with an 8 KiB output tail
/tasks cancel <id> — cancel a task
/skills — list skills available to the session
/skills run <name> [args] — activate an exact skill
/mcp — list session-derived MCP tools
/compact — compact session context and report event metrics
/undo [count] — undo one or more history steps
/goal [status|pause|resume|cancel|-- <objective>|<objective>] — inspect or control a goal
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


@dataclass(frozen=True, slots=True)
class _CompactionOutcome:
    state: Literal["completed", "blocked", "cancelled"]
    compacted_count: int | None = None
    tokens_before: int | None = None
    tokens_after: int | None = None


@dataclass(slots=True)
class _CompactionWaiter:
    future: asyncio.Future[_CompactionOutcome]
    active_trigger: Literal["manual", "auto"] | None = None


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
        self._compaction_waiters: dict[str, _CompactionWaiter] = {}
        self._interaction_lock = asyncio.Lock()

    async def close(self) -> None:
        self._fail_all_compaction_waiters(
            KimiServerError("kimi event stream stopped")
        )
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
                try:
                    await self._handle_command(
                        conversation_key,
                        adapter,
                        msg.conversation,
                        msg.actor,
                        text,
                    )
                except KimiServerError as exc:
                    await self._send_chunked(
                        adapter, conversation=msg.conversation, text=f"Command failed: {exc}"
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

        if isinstance(event_type, str) and event_type.startswith("compaction."):
            self._dispatch_compaction_event(active.session_id, event_type, payload)
            return

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
        if command == "/model":
            await self._handle_model(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/effort":
            await self._handle_effort(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/plan":
            await self._handle_plan(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/status":
            if argument:
                await self._send_chunked(
                    adapter, conversation, "Usage: /status"
                )
                return
            await self._handle_status(conversation_key, adapter, conversation)
            return
        if command == "/title":
            await self._handle_title(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/usage":
            if argument:
                await self._send_chunked(
                    adapter, conversation, "Usage: /usage"
                )
                return
            await self._handle_usage(conversation_key, adapter, conversation)
            return
        if command == "/tasks":
            await self._handle_tasks(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/skills":
            await self._handle_skills(
                conversation_key,
                adapter,
                conversation,
                actor,
                argument,
            )
            return
        if command == "/mcp":
            if argument:
                await self._send_chunked(adapter, conversation, "Usage: /mcp")
                return
            await self._handle_mcp(conversation_key, adapter, conversation)
            return
        if command == "/compact":
            await self._handle_compact(
                conversation_key,
                adapter,
                conversation,
                actor,
                argument,
            )
            return
        if command == "/undo":
            await self._handle_undo(
                conversation_key, adapter, conversation, argument
            )
            return
        if command == "/goal":
            await self._handle_goal(
                conversation_key,
                adapter,
                conversation,
                actor,
                argument,
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
            binding = await self._require_binding(
                conversation_key, adapter, conversation
            )
            if binding is None:
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
            binding = await self._require_binding(
                conversation_key, adapter, conversation
            )
            if binding is None:
                return
            aborted, cancelled_interaction = await self._cancel_active_work(
                conversation_key,
                binding.session_id,
                detail="Cancelled by /stop.",
            )
            result = (
                "Stopped."
                if aborted or cancelled_interaction
                else "No active prompt."
            )
            await self._send_chunked(adapter, conversation, result)
            return

        await self._send_chunked(
            adapter, conversation, f"Unknown command: {command}\nUse /help."
        )

    async def _handle_model(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument:
            profile, status, models = await asyncio.gather(
                self._client.get_session_profile(binding.session_id),
                self._client.get_session_status(binding.session_id),
                self._client.list_models(),
            )
            await self._send_chunked(
                adapter,
                conversation,
                _format_models(
                    _effective_model(profile, status, self._model), models
                ),
            )
            return

        status = await self._require_idle(
            binding, adapter, conversation, "Model changes"
        )
        if status is None:
            return
        models = await self._client.list_models()
        selected = next(
            (model for model in models if model.alias == argument), None
        )
        if selected is None:
            await self._send_chunked(
                adapter,
                conversation,
                f"Unknown model alias: {argument}\nUse /model to list exact aliases.",
            )
            return

        current_effort = status.thinking_effort
        choices = _model_effort_choices(selected)
        if current_effort in choices:
            next_effort = current_effort
        elif selected.default_effort in selected.support_efforts:
            assert selected.default_effort is not None
            next_effort = selected.default_effort
        elif not selected.support_efforts:
            next_effort = "on" if _model_supports_thinking(selected) else "off"
        else:
            raise KimiServerProtocolError(
                f"model {selected.alias} advertises efforts without a valid default"
            )
        await self._client.update_profile(
            binding.session_id,
            model=selected.alias,
            thinking=next_effort,
        )
        lines = [f"Model: {selected.alias}"]
        if next_effort != current_effort:
            lines.append(
                f"Thinking effort adjusted: {current_effort} -> {next_effort}"
            )
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _handle_effort(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument:
            profile, status, models = await asyncio.gather(
                self._client.get_session_profile(binding.session_id),
                self._client.get_session_status(binding.session_id),
                self._client.list_models(),
            )
            model = _find_model(
                models, _effective_model(profile, status, self._model)
            )
            choices = ", ".join(_model_effort_choices(model))
            await self._send_chunked(
                adapter,
                conversation,
                f"Thinking effort: {status.thinking_effort}\nValid choices: {choices}",
            )
            return

        status = await self._require_idle(
            binding, adapter, conversation, "Thinking-effort changes"
        )
        if status is None:
            return
        profile, models = await asyncio.gather(
            self._client.get_session_profile(binding.session_id),
            self._client.list_models(),
        )
        model = _find_model(
            models, _effective_model(profile, status, self._model)
        )
        choices = _model_effort_choices(model)
        if argument not in choices:
            await self._send_chunked(
                adapter,
                conversation,
                f"Unsupported effort for {model.alias}: {argument}\nValid choices: {', '.join(choices)}",
            )
            return
        if argument == status.thinking_effort:
            await self._send_chunked(
                adapter,
                conversation,
                f"Thinking effort already: {argument}",
            )
            return
        await self._client.update_profile(
            binding.session_id, thinking=argument
        )
        await self._send_chunked(
            adapter, conversation, f"Thinking effort: {argument}"
        )

    async def _handle_plan(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        if argument not in {"", "on", "off"}:
            await self._send_chunked(
                adapter, conversation, "Usage: /plan [on|off]"
            )
            return
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument:
            status = await self._client.get_session_status(binding.session_id)
            await self._send_chunked(
                adapter,
                conversation,
                f"Current plan mode: {'on' if status.plan_mode else 'off'}",
            )
            return
        status = await self._require_idle(
            binding, adapter, conversation, "Plan-mode changes"
        )
        if status is None:
            return
        enabled = argument == "on"
        if status.plan_mode == enabled:
            await self._send_chunked(
                adapter, conversation, f"Plan mode already: {argument}"
            )
            return
        await self._client.update_profile(
            binding.session_id, plan_mode=enabled
        )
        await self._send_chunked(
            adapter, conversation, f"Plan mode: {argument}"
        )

    async def _handle_status(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        profile, status, server_version = await asyncio.gather(
            self._client.get_session_profile(binding.session_id),
            self._client.get_session_status(binding.session_id),
            self._client.get_server_version(),
        )
        await self._send_chunked(
            adapter,
            conversation,
            _format_status(
                profile,
                status,
                binding.permission_mode,
                server_version,
                self._model,
            ),
        )

    async def _handle_title(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument:
            profile = await self._client.get_session_profile(binding.session_id)
            await self._send_chunked(
                adapter, conversation, f"Title: {profile.title}"
            )
            return
        await self._client.update_profile(binding.session_id, title=argument)
        await self._send_chunked(adapter, conversation, f"Title: {argument}")

    async def _handle_usage(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        usage = await self._client.get_session_usage(binding.session_id)
        await self._send_chunked(
            adapter, conversation, _format_usage(usage)
        )

    async def _handle_tasks(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument or argument in TASK_STATUSES:
            status = cast(TaskStatus, argument) if argument else None
            tasks = await self._client.list_tasks(
                binding.session_id, status=status
            )
            await self._send_chunked(
                adapter, conversation, _format_tasks(tasks, status)
            )
            return
        parts = argument.split()
        if len(parts) == 2 and parts[0] == "show":
            task = await self._client.get_task(
                binding.session_id,
                parts[1],
                output_bytes=TASK_OUTPUT_BYTES,
            )
            await self._send_chunked(
                adapter, conversation, _format_task_detail(task)
            )
            return
        if len(parts) == 2 and parts[0] == "cancel":
            await self._client.cancel_task(binding.session_id, parts[1])
            await self._send_chunked(
                adapter, conversation, f"Cancelled task {parts[1]}"
            )
            return
        await self._send_chunked(
            adapter,
            conversation,
            "Usage: /tasks [running|completed|failed|cancelled] | /tasks show <id> | /tasks cancel <id>",
        )

    async def _handle_skills(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        if not argument:
            skills = await self._client.list_skills(binding.session_id)
            await self._send_chunked(
                adapter, conversation, _format_skills(skills)
            )
            return

        verb, _, activation = argument.partition(" ")
        activation = activation.strip()
        if verb != "run" or not activation:
            await self._send_chunked(
                adapter,
                conversation,
                "Usage: /skills run <name> [args]",
            )
            return
        skill_name, _, args = activation.partition(" ")
        status = await self._require_idle(
            binding, adapter, conversation, "Skill activation"
        )
        if status is None:
            return
        skills = await self._client.list_skills(binding.session_id)
        if not any(skill.name == skill_name for skill in skills):
            await self._send_chunked(
                adapter,
                conversation,
                f"Unknown skill: {skill_name}\nUse /skills to list exact names.",
            )
            return
        await self._ensure_active_stream(
            conversation_key,
            binding.session_id,
            adapter,
            conversation,
            actor,
        )
        await self._client.activate_skill(
            binding.session_id, skill_name, args=args.strip()
        )

    async def _handle_mcp(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        tools = await self._client.list_tools(binding.session_id)
        await self._send_chunked(
            adapter, conversation, _format_mcp_tools(tools)
        )

    async def _handle_compact(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        if argument:
            await self._send_chunked(adapter, conversation, "Usage: /compact")
            return
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        status = await self._require_idle(
            binding, adapter, conversation, "Compaction"
        )
        if status is None:
            return

        progress = await adapter.send_text(conversation, "Compacting...")
        if binding.session_id in self._compaction_waiters:
            await adapter.edit_text(
                progress,
                "Compaction failed: another compaction is already being tracked.",
            )
            return
        future = asyncio.get_running_loop().create_future()
        waiter = _CompactionWaiter(future=future)
        self._compaction_waiters[binding.session_id] = waiter
        try:
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                conversation,
                actor,
            )
            await self._client.compact_session(binding.session_id)
            outcome = await future
            if outcome.state == "completed":
                assert outcome.compacted_count is not None
                assert outcome.tokens_before is not None
                assert outcome.tokens_after is not None
                text = (
                    f"Compaction complete: {outcome.compacted_count} prompts compacted; "
                    f"tokens {outcome.tokens_before} -> {outcome.tokens_after}."
                )
            elif outcome.state == "blocked":
                text = "Compaction failed: Kimi blocked the compaction."
            else:
                text = "Compaction failed: Kimi cancelled the compaction."
        except Exception as exc:
            text = f"Compaction failed: {exc}"
        finally:
            if self._compaction_waiters.get(binding.session_id) is waiter:
                self._compaction_waiters.pop(binding.session_id)
            if future.done() and not future.cancelled():
                future.exception()
        await adapter.edit_text(progress, text)

    async def _handle_undo(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        parts = argument.split()
        if not parts:
            count = 1
        elif len(parts) == 1 and parts[0].isascii() and parts[0].isdecimal():
            count = int(parts[0])
            if count == 0:
                await self._send_chunked(
                    adapter, conversation, "Usage: /undo [positive-count]"
                )
                return
        else:
            await self._send_chunked(
                adapter, conversation, "Usage: /undo [positive-count]"
            )
            return
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return
        status = await self._require_idle(
            binding, adapter, conversation, "Undo"
        )
        if status is None:
            return
        await self._client.undo_session(binding.session_id, count=count)
        await self._send_chunked(
            adapter,
            conversation,
            f"Undid {count} history {'step' if count == 1 else 'steps'}.",
        )

    async def _handle_goal(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(
            conversation_key, adapter, conversation
        )
        if binding is None:
            return

        if not argument or argument == "status":
            goal = await self._client.get_goal(binding.session_id)
            await self._send_chunked(
                adapter, conversation, _format_goal(goal)
            )
            return

        first, separator, remainder = argument.partition(" ")
        if first in {"status", "pause", "resume", "cancel"} and separator:
            await self._send_chunked(
                adapter,
                conversation,
                "Objectives beginning with status, pause, resume, or cancel must use /goal -- <objective>.",
            )
            return

        if argument in {"pause", "cancel"}:
            goal = await self._client.get_goal(binding.session_id)
            if goal is None:
                await self._send_chunked(
                    adapter, conversation, "No active goal."
                )
                return
            await self._cancel_active_work(
                conversation_key,
                binding.session_id,
                detail=f"Cancelled by /goal {argument}.",
            )
            await self._client.update_profile(
                binding.session_id, goal_control=cast(GoalControl, argument)
            )
            await self._send_chunked(
                adapter,
                conversation,
                "Goal paused." if argument == "pause" else "Goal cancelled.",
            )
            return

        if argument == "resume":
            goal = await self._client.get_goal(binding.session_id)
            if goal is None:
                await self._send_chunked(
                    adapter, conversation, "No active goal."
                )
                return
            status = await self._require_idle(
                binding, adapter, conversation, "Goal resume"
            )
            if status is None:
                return
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                conversation,
                actor,
            )
            await self._client.update_profile(
                binding.session_id, goal_control="resume"
            )
            await self._send_chunked(adapter, conversation, "Goal resumed.")
            return

        if first == "--":
            objective = remainder.strip()
            if not objective:
                await self._send_chunked(
                    adapter, conversation, "Usage: /goal -- <objective>"
                )
                return
        else:
            objective = argument

        goal = await self._client.get_goal(binding.session_id)
        if goal is not None:
            await self._send_chunked(
                adapter,
                conversation,
                "A goal already exists. Pause, resume, or cancel it explicitly.",
            )
            return
        status = await self._require_idle(
            binding, adapter, conversation, "Goal creation"
        )
        if status is None:
            return
        await self._ensure_active_stream(
            conversation_key,
            binding.session_id,
            adapter,
            conversation,
            actor,
        )
        await self._client.update_profile(
            binding.session_id, goal_objective=objective
        )
        result = await self._client.submit_prompt(
            binding.session_id,
            objective,
            permission_mode=binding.permission_mode,
        )
        if result.get("status") in {"queued", "blocked"}:
            prompt_id = str(result["prompt_id"])
            try:
                await self._client.steer_prompts(
                    binding.session_id, [prompt_id]
                )
            except KimiServerAPIError as exc:
                if exc.code != 40001:
                    raise

    async def _cancel_active_work(
        self,
        conversation_key: str,
        session_id: str,
        *,
        detail: str,
    ) -> tuple[bool, bool]:
        async with self._interaction_lock:
            pending = self._pending.get(conversation_key)
            session_ids: list[str] = []
            if pending is not None:
                session_ids.append(pending.session_id)
            if session_id not in session_ids:
                session_ids.append(session_id)
            aborted = False
            for target_session_id in session_ids:
                aborted = (
                    await self._client.abort_prompt(target_session_id)
                    or aborted
                )
            if pending is not None:
                await self._clear_pending(pending)
                await pending.adapter.finish_interaction(
                    pending.message,
                    InteractionOutcome(state="cancelled", detail=detail),
                )
            return aborted, pending is not None

    async def _require_binding(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> ConversationBinding | None:
        binding = self._state.bindings.get(conversation_key)
        if binding is None:
            await self._send_chunked(adapter, conversation, "No bound session.")
        return binding

    async def _require_idle(
        self,
        binding: ConversationBinding,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        operation: str,
    ) -> SessionStatus | None:
        status = await self._client.get_session_status(binding.session_id)
        if status.busy:
            await self._send_chunked(
                adapter,
                conversation,
                f"Session is busy. {operation} can only run while idle.",
            )
            return None
        return status

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
            model=self._model,
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
        failure: KimiServerError | None = None
        try:
            async for event in self._client.subscribe_events(active.session_id):
                if self._active is not active:
                    return
                await self.dispatch_event(active.conversation_key, event)
        except asyncio.CancelledError:
            failure = KimiServerError("kimi event stream stopped")
            raise
        except Exception as exc:
            failure = KimiServerError(f"kimi event stream failed: {exc}")
            raise
        else:
            failure = KimiServerError("kimi event stream ended")
        finally:
            if failure is None:
                failure = KimiServerError("kimi event stream stopped")
            self._fail_compaction_waiter(active.session_id, failure)

    def _dispatch_compaction_event(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is None or waiter.future.done():
            return
        if event_type == "compaction.started":
            trigger = payload.get("trigger")
            if trigger in {"manual", "auto"}:
                waiter.active_trigger = cast(Literal["manual", "auto"], trigger)
            return
        if event_type not in {
            "compaction.completed",
            "compaction.blocked",
            "compaction.cancelled",
        }:
            return
        if waiter.active_trigger == "auto":
            waiter.active_trigger = None
            return
        if waiter.active_trigger != "manual":
            return

        if event_type == "compaction.completed":
            result = payload.get("result")
            if not isinstance(result, dict):
                waiter.future.set_exception(
                    KimiServerProtocolError(
                        "compaction.completed event has no result"
                    )
                )
                return
            try:
                outcome = _CompactionOutcome(
                    state="completed",
                    compacted_count=int(result["compactedCount"]),
                    tokens_before=int(result["tokensBefore"]),
                    tokens_after=int(result["tokensAfter"]),
                )
            except (KeyError, TypeError, ValueError):
                waiter.future.set_exception(
                    KimiServerProtocolError(
                        "compaction.completed event has invalid metrics"
                    )
                )
                return
        elif event_type == "compaction.blocked":
            outcome = _CompactionOutcome(state="blocked")
        else:
            outcome = _CompactionOutcome(state="cancelled")
        waiter.future.set_result(outcome)

    def _fail_compaction_waiter(
        self, session_id: str, error: KimiServerError
    ) -> None:
        waiter = self._compaction_waiters.get(session_id)
        if waiter is not None and not waiter.future.done():
            waiter.future.set_exception(error)

    def _fail_all_compaction_waiters(self, error: KimiServerError) -> None:
        for session_id in tuple(self._compaction_waiters):
            self._fail_compaction_waiter(session_id, error)

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


def _format_models(current_model: str, models: list[ModelInfo]) -> str:
    lines = [f"Current model: {current_model}", "Available models:"]
    for model in models:
        display_name = model.display_name or "display name unavailable"
        efforts = ", ".join(_model_effort_choices(model))
        lines.append(
            f"- {model.alias} — {display_name} — thinking efforts: {efforts}"
        )
    return "\n".join(lines)


def _model_effort_choices(model: ModelInfo) -> tuple[str, ...]:
    efforts = tuple(dict.fromkeys(model.support_efforts))
    always_thinking = "always_thinking" in model.capabilities
    if efforts:
        return efforts if always_thinking else ("off", *efforts)
    if always_thinking:
        return ("on",)
    if _model_supports_thinking(model):
        return ("on", "off")
    return ("off",)


def _model_supports_thinking(model: ModelInfo) -> bool:
    return bool(
        {"thinking", "always_thinking"}.intersection(model.capabilities)
    )


def _find_model(models: list[ModelInfo], alias: str) -> ModelInfo:
    model = next((item for item in models if item.alias == alias), None)
    if model is None:
        raise KimiServerProtocolError(
            f"session model {alias!r} is absent from the model catalog"
        )
    return model


def _effective_model(
    profile: SessionProfile, status: SessionStatus, default_model: str
) -> str:
    return status.model or profile.model or default_model


def _format_status(
    profile: SessionProfile,
    status: SessionStatus,
    permission_mode: str,
    server_version: str,
    default_model: str,
) -> str:
    return "\n".join(
        (
            f"Session: {profile.title}",
            f"ID: {profile.session_id}",
            f"Workspace: {profile.workspace}",
            f"State: {'busy' if status.busy else 'idle'}",
            f"Pending interaction: {profile.pending_interaction}",
            f"Model: {_effective_model(profile, status, default_model)}",
            f"Thinking effort: {status.thinking_effort}",
            f"Plan mode: {'on' if status.plan_mode else 'off'}",
            f"Permission mode: {permission_mode}",
            f"Kimi-code: {server_version}",
        )
    )


def _format_usage(usage: SessionUsage) -> str:
    if usage.context_tokens is None or usage.context_limit is None:
        context = "unknown"
    elif usage.context_limit <= 0:
        context = f"{usage.context_tokens}/{usage.context_limit} (unknown percentage)"
    else:
        percentage = usage.context_tokens / usage.context_limit * 100
        context = (
            f"{usage.context_tokens}/{usage.context_limit} ({percentage:.1f}%)"
        )
    return "\n".join(
        (
            f"Input tokens: {_optional_number(usage.input_tokens)}",
            f"Output tokens: {_optional_number(usage.output_tokens)}",
            f"Cache-read tokens: {_optional_number(usage.cache_read_tokens)}",
            f"Cache-creation tokens: {_optional_number(usage.cache_creation_tokens)}",
            f"Context: {context}",
        )
    )


def _optional_number(value: int | None) -> str:
    return str(value) if value is not None else "unknown"


def _format_tasks(tasks: list[TaskInfo], status: TaskStatus | None) -> str:
    if not tasks:
        suffix = f" with status {status}" if status is not None else ""
        return f"No tasks found{suffix}."
    lines: list[str] = []
    for task in tasks:
        lines.append(
            f"{task.id} [{task.status}] {task.kind}\n{task.description}"
        )
    return "\n\n".join(lines)


def _format_task_detail(task: TaskInfo) -> str:
    lines = [
        f"Task: {task.id}",
        f"Status: {task.status}",
        f"Kind: {task.kind}",
        f"Description: {task.description}",
    ]
    if task.command is not None:
        lines.append(f"Command: {task.command}")
    lines.append(f"Created: {task.created_at}")
    if task.started_at is not None:
        lines.append(f"Started: {task.started_at}")
    if task.completed_at is not None:
        lines.append(f"Completed: {task.completed_at}")
    if task.output_bytes is not None:
        lines.append(f"Output bytes: {task.output_bytes}")
    if task.output_preview is not None:
        lines.extend(("Output tail:", task.output_preview))
    else:
        lines.append("Output tail: unavailable")
    return "\n".join(lines)


def _format_goal(goal: GoalInfo | None) -> str:
    if goal is None:
        return "No active goal."
    budget = goal.budget
    lines = [
        f"Goal: {goal.objective}",
        f"Status: {goal.status}",
    ]
    if goal.completion_criterion is not None:
        lines.append(f"Completion criterion: {goal.completion_criterion}")
    lines.extend(
        (
            f"Used: {goal.turns_used} turns; {goal.tokens_used} tokens; {_format_milliseconds(goal.wall_clock_ms)}",
            "Budgets:",
            _format_goal_budget_line(
                "Tokens",
                budget.token_budget,
                budget.remaining_tokens,
                budget.token_budget_reached,
            ),
            _format_goal_budget_line(
                "Turns",
                budget.turn_budget,
                budget.remaining_turns,
                budget.turn_budget_reached,
            ),
            _format_goal_budget_line(
                "Time",
                budget.wall_clock_budget_ms,
                budget.remaining_wall_clock_ms,
                budget.wall_clock_budget_reached,
                milliseconds=True,
            ),
            f"Over budget: {'yes' if budget.over_budget else 'no'}",
        )
    )
    if goal.terminal_reason is not None:
        lines.append(f"Terminal reason: {goal.terminal_reason}")
    return "\n".join(lines)


def _format_goal_budget_line(
    label: str,
    limit: int | None,
    remaining: int | None,
    reached: bool,
    *,
    milliseconds: bool = False,
) -> str:
    if milliseconds:
        limit_text = _format_milliseconds(limit) if limit is not None else "not set"
        remaining_text = (
            _format_milliseconds(remaining) if remaining is not None else "not set"
        )
    else:
        limit_text = str(limit) if limit is not None else "not set"
        remaining_text = str(remaining) if remaining is not None else "not set"
    return (
        f"- {label}: limit {limit_text}; remaining {remaining_text}; "
        f"reached {'yes' if reached else 'no'}"
    )


def _format_milliseconds(value: int) -> str:
    if value < 1000:
        return f"{value} ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:g} s"
    minutes, remaining_seconds = divmod(seconds, 60)
    if remaining_seconds:
        return f"{int(minutes)} min {remaining_seconds:g} s"
    return f"{int(minutes)} min"


def _format_skills(skills: list[SkillInfo]) -> str:
    if not skills:
        return "No skills available."
    return "\n\n".join(
        f"{skill.name} [{skill.source}]\n{skill.description}"
        for skill in skills
    )


def _format_mcp_tools(tools: list[ToolInfo]) -> str:
    grouped: dict[str, list[ToolInfo]] = {}
    for tool in tools:
        if tool.source != "mcp" or not tool.mcp_server_id:
            continue
        grouped.setdefault(tool.mcp_server_id, []).append(tool)
    if not grouped:
        return "No MCP tools available for this session."
    sections: list[str] = []
    for server_id in sorted(grouped):
        lines = [server_id]
        for tool in sorted(grouped[server_id], key=lambda item: item.name):
            description = f" — {tool.description}" if tool.description else ""
            lines.append(f"- {tool.name}{description}")
        sections.append("\n".join(lines))
    return "MCP servers:\n" + "\n\n".join(sections)


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
