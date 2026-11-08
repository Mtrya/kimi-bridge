"""Native queue, swarm, and conversation lifecycle commands."""

from __future__ import annotations

from ..kimi_server import PromptDelivery, PromptOutcome
from ..platforms.base import ActorRef, ConversationRef, PlatformAdapter
from .formatting import _format_prompt_queue


class _SessionControlsMixin:
    async def _handle_queue(
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
        choice_key = (conversation_key, conversation)
        if not argument:
            queue = await self._client.get_prompt_queue(binding.session_id)
            self._queue_choices.pop(choice_key, None)
            await self._send_chunked(adapter, conversation, _format_prompt_queue(queue))
            self._queue_choices[choice_key] = (
                binding.session_id,
                tuple(prompt.id for prompt in queue.waiting),
            )
            return
        verb, _, remainder = argument.partition(" ")
        if verb in {"cancel", "steer"}:
            selected = remainder.strip()
            explicit = selected.startswith("id ")
            if explicit:
                selected = selected[3:].strip()
            if not selected or " " in selected:
                await self._send_chunked(
                    adapter, conversation, f"Usage: /queue {verb} <n|id>"
                )
                return
            if selected.isdecimal() and not explicit:
                choices = self._queue_choices.get(choice_key)
                if (
                    choices is None
                    or choices[0] != binding.session_id
                    or len(selected) > len(str(len(choices[1])))
                    or not 1 <= int(selected) <= len(choices[1])
                ):
                    await self._send_chunked(
                        adapter,
                        conversation,
                        "Queue number unavailable. Run /queue again.",
                    )
                    return
                selected = choices[1][int(selected) - 1]
            if verb == "cancel":
                cancelled = await self._client.cancel_prompt(
                    binding.session_id, selected
                )
                text = (
                    f"Cancelled prompt {selected}"
                    if cancelled
                    else f"Prompt {selected} already ended."
                )
            else:
                await self._client.steer_prompt(binding.session_id, selected)
                text = f"Steered prompt {selected}"
            await self._send_chunked(adapter, conversation, text)
            return
        message = remainder.strip() if verb == "--" else argument
        if not message:
            await self._send_chunked(adapter, conversation, "Usage: /queue <message>")
            return
        await self._ensure_active_stream(
            conversation_key, binding.session_id, adapter, conversation, actor
        )
        result = await self._client.submit_prompt(
            binding.session_id,
            message,
            delivery=PromptDelivery.ENQUEUE,
            permission_mode=binding.permission_mode,
        )
        await self._report_prompt_outcome(
            adapter, conversation, result, operation="Queued prompt"
        )
        if result.outcome == PromptOutcome.SUBMITTED:
            await self._send_chunked(
                adapter, conversation, f"Submitted prompt {result.prompt_id}"
            )

    async def _handle_swarm(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        if argument not in {"", "on", "off"}:
            await self._send_chunked(adapter, conversation, "Usage: /swarm [on|off]")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        if argument:
            await self._client.update_profile(
                binding.session_id, swarm_mode=argument == "on"
            )
            enabled = argument == "on"
        else:
            enabled = (
                await self._client.inspect_session(binding.session_id)
            ).swarm_mode
        await self._send_chunked(
            adapter, conversation, f"Swarm mode: {'on' if enabled else 'off'}"
        )

    async def _handle_fork(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        if argument:
            await self._send_chunked(adapter, conversation, "Usage: /fork")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        session_id = await self._client.fork_session(binding.session_id)
        session = await self._client.get_session(session_id)
        await self._bind_selected_session(
            conversation_key, session, adapter, conversation, actor
        )
        await self._send_chunked(
            adapter,
            conversation,
            f"Forked to {session_id}\nWorkspace: {session['metadata']['cwd']}\n"
            "The source can continue running; its in-progress answer is excluded.",
        )

    async def _handle_restore(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        actor: ActorRef,
        argument: str,
    ) -> None:
        explicit = argument.startswith("id ")
        selected = argument[3:].strip() if explicit else argument
        if not selected:
            await self._send_chunked(adapter, conversation, "Usage: /restore <n|id>")
            return
        if selected.isdecimal() and not explicit:
            session = await self._resolve_session(conversation_key, selected)
            if session is None or not session.get("archived"):
                await self._send_chunked(
                    adapter,
                    conversation,
                    "Archived session number unavailable. Run /sessions archived.",
                )
                return
            selected = str(session["id"])
        session_id = await self._client.restore_session(selected)
        session = await self._client.get_session(session_id)
        await self._bind_selected_session(
            conversation_key, session, adapter, conversation, actor
        )
        recap = await self._history_recap(session_id)
        text = f"Restored and switched to {session_id}"
        if recap is not None:
            text += f"\n\n{recap}"
        await self._send_chunked(adapter, conversation, text)
