"""Bridge command parsing and command-specific orchestration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

from ..kimi_server import (
    GoalControl,
    KimiServerAPIError,
    KimiServerError,
    KimiServerProtocolError,
    TaskStatus,
)
from ..platforms.base import ActorRef, ConversationRef, PlatformAdapter
from ..state import PERMISSION_MODES, ConversationBinding
from .files import _load_outbound_file
from .formatting import (
    _effective_model,
    _find_model,
    _format_goal,
    _format_mcp_tools,
    _format_models,
    _format_sessions,
    _format_skills,
    _format_status,
    _format_task_detail,
    _format_tasks,
    _format_usage,
    _model_effort_choices,
    _model_supports_thinking,
)
from .models import _CompactionWaiter


TASK_OUTPUT_BYTES = 8 * 1024
TASK_STATUSES: frozenset[str] = frozenset(
    {"running", "completed", "failed", "cancelled"}
)
PERMISSION_MODE_DESCRIPTIONS = {
    "manual": "Approvals and questions can be answered in chat.",
    "auto": "Fully autonomous; the agent never asks questions.",
    "yolo": "Regular tools are auto-approved; the agent may still ask questions.",
}

HELP_TEXT = """**Commands**

**Sessions**
- **/new [cwd]** — create and bind a session
- **/sessions** — list recent sessions
- **/switch <n|id>** — bind a listed or explicit session
- **/status** — show bound session and runtime state
- **/title [text]** — show or rename the session
- **/usage** — show live session token totals and context usage
- **/compact** — compact session context and report event metrics
- **/undo [count]** — undo one or more history steps

**Control**
- **/mode <manual|auto|yolo>** — manual uses chat interactions; auto never asks; yolo may ask questions
- **/model [alias]** — show or set the exact session model
- **/effort [effort]** — show or set thinking effort for the current model
- **/plan [on|off]** — show or explicitly set plan mode
- **/goal [status|pause|resume|cancel|-- <objective>|<objective>]** — inspect or control a goal
- **/stop** — stop the active turn and discard queued prompts

**Tasks and tools**
- **/tasks [running|completed|failed|cancelled]** — list tasks
- **/tasks show <id>** — inspect a task with an 8 KiB output tail
- **/tasks cancel <id>** — cancel a task
- **/skills** — list skills available to the session
- **/skills run <name> [args]** — activate an exact skill
- **/mcp** — list session-derived MCP tools

**Output**
- **/send <path>** — send one file from the bound workspace
- **/render-thinking [on|off]** — show or set separate thinking output

**General**
- **/help** — show this help"""


class _CommandMixin:
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
            await self._send_chunked(adapter, conversation, _format_sessions(sessions))
            return
        if command == "/switch":
            if not argument:
                await self._send_chunked(adapter, conversation, "Usage: /switch <n|id>")
                return
            session = await self._resolve_session(conversation_key, argument)
            if session is None:
                await self._send_chunked(
                    adapter, conversation, f"Session not found: {argument}"
                )
                return
            current = self._state.bindings.get(conversation_key)
            binding = self._binding_from_session(
                session,
                render_thinking=(
                    current.render_thinking if current is not None else False
                ),
            )
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
            await self._handle_model(conversation_key, adapter, conversation, argument)
            return
        if command == "/effort":
            await self._handle_effort(conversation_key, adapter, conversation, argument)
            return
        if command == "/plan":
            await self._handle_plan(conversation_key, adapter, conversation, argument)
            return
        if command == "/status":
            if argument:
                await self._send_chunked(adapter, conversation, "Usage: /status")
                return
            await self._handle_status(conversation_key, adapter, conversation)
            return
        if command == "/title":
            await self._handle_title(conversation_key, adapter, conversation, argument)
            return
        if command == "/usage":
            if argument:
                await self._send_chunked(adapter, conversation, "Usage: /usage")
                return
            await self._handle_usage(conversation_key, adapter, conversation)
            return
        if command == "/tasks":
            await self._handle_tasks(conversation_key, adapter, conversation, argument)
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
            await self._handle_undo(conversation_key, adapter, conversation, argument)
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
        if command == "/send":
            await self._handle_send(conversation_key, adapter, conversation, argument)
            return
        if command == "/render-thinking":
            await self._handle_render_thinking(
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
                render_thinking=binding.render_thinking,
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
            await self._cancel_active_work(
                conversation_key,
                binding.session_id,
                detail="Cancelled by /stop.",
                session_wide=True,
            )
            await self._send_chunked(adapter, conversation, "Stopped.")
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
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
                _format_models(_effective_model(profile, status, self._model), models),
            )
            return

        status = await self._require_idle(
            binding, adapter, conversation, "Model changes"
        )
        if status is None:
            return
        models = await self._client.list_models()
        selected = next((model for model in models if model.alias == argument), None)
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
            lines.append(f"Thinking effort adjusted: {current_effort} -> {next_effort}")
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _handle_effort(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if not argument:
            profile, status, models = await asyncio.gather(
                self._client.get_session_profile(binding.session_id),
                self._client.get_session_status(binding.session_id),
                self._client.list_models(),
            )
            model = _find_model(models, _effective_model(profile, status, self._model))
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
        model = _find_model(models, _effective_model(profile, status, self._model))
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
        await self._client.update_profile(binding.session_id, thinking=argument)
        await self._send_chunked(adapter, conversation, f"Thinking effort: {argument}")

    async def _handle_plan(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        if argument not in {"", "on", "off"}:
            await self._send_chunked(adapter, conversation, "Usage: /plan [on|off]")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
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
        await self._client.update_profile(binding.session_id, plan_mode=enabled)
        await self._send_chunked(adapter, conversation, f"Plan mode: {argument}")

    async def _handle_status(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if not argument:
            profile = await self._client.get_session_profile(binding.session_id)
            await self._send_chunked(adapter, conversation, f"Title: {profile.title}")
            return
        await self._client.update_profile(binding.session_id, title=argument)
        await self._send_chunked(adapter, conversation, f"Title: {argument}")

    async def _handle_usage(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        usage = await self._client.get_session_usage(binding.session_id)
        await self._send_chunked(adapter, conversation, _format_usage(usage))

    async def _handle_send(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        if not argument:
            await self._send_chunked(adapter, conversation, "Usage: /send <path>")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        try:
            outbound = _load_outbound_file(Path(binding.workspace), argument)
        except (OSError, ValueError) as exc:
            await self._send_chunked(adapter, conversation, str(exc))
            return
        try:
            await adapter.send_file(conversation, outbound)
        except Exception as exc:
            await self._send_chunked(adapter, conversation, f"File send failed: {exc}")

    async def _handle_render_thinking(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        if argument not in {"", "on", "off"}:
            await self._send_chunked(
                adapter, conversation, "Usage: /render-thinking [on|off]"
            )
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if not argument:
            state = "on" if binding.render_thinking else "off"
            await self._send_chunked(
                adapter, conversation, f"Thinking rendering: {state}"
            )
            return

        enabled = argument == "on"
        changed = binding.render_thinking != enabled
        if changed:
            binding = ConversationBinding(
                session_id=binding.session_id,
                workspace=binding.workspace,
                permission_mode=binding.permission_mode,
                render_thinking=enabled,
            )
            self._state.bindings[conversation_key] = binding
            self._state_store.save(self._state)

        if enabled:
            await self._ensure_active_stream(
                conversation_key,
                binding.session_id,
                adapter,
                conversation,
                actor,
            )
            await self._backfill_thinking(self._active)
        else:
            active = self._active
            if active is not None and active.conversation_key == conversation_key:
                await self._cancel_delayed_flush(active.thinking)

        qualifier = "" if changed else " already"
        await self._send_chunked(
            adapter,
            conversation,
            f"Thinking rendering{qualifier}: {argument}",
        )

    async def _handle_tasks(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if not argument or argument in TASK_STATUSES:
            status = cast(TaskStatus, argument) if argument else None
            tasks = await self._client.list_tasks(binding.session_id, status=status)
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
            await self._send_chunked(adapter, conversation, _format_task_detail(task))
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if not argument:
            skills = await self._client.list_skills(binding.session_id)
            await self._send_chunked(adapter, conversation, _format_skills(skills))
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        tools = await self._client.list_tools(binding.session_id)
        await self._send_chunked(adapter, conversation, _format_mcp_tools(tools))

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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        status = await self._require_idle(binding, adapter, conversation, "Compaction")
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        status = await self._require_idle(binding, adapter, conversation, "Undo")
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
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return

        if not argument or argument == "status":
            goal = await self._client.get_goal(binding.session_id)
            await self._send_chunked(adapter, conversation, _format_goal(goal))
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
                await self._send_chunked(adapter, conversation, "No active goal.")
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
                await self._send_chunked(adapter, conversation, "No active goal.")
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
            await self._client.update_profile(binding.session_id, goal_control="resume")
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
        await self._client.update_profile(binding.session_id, goal_objective=objective)
        result = await self._client.submit_prompt(
            binding.session_id,
            objective,
            permission_mode=binding.permission_mode,
        )
        if result.get("status") in {"queued", "blocked"}:
            prompt_id = str(result["prompt_id"])
            try:
                await self._client.steer_prompts(binding.session_id, [prompt_id])
            except KimiServerAPIError as exc:
                if exc.code != 40001:
                    raise
