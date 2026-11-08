"""Live native activity inspection and selected background controls."""

from __future__ import annotations

from ..kimi_server import TodoItem
from ..platforms.base import ConversationRef, PlatformAdapter


class _ActivityMixin:
    async def _handle_agents(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        activity = await self._client.inspect_activity(binding.session_id)
        choice_key = (conversation_key, conversation)
        if not argument:
            self._agent_choices.pop(choice_key, None)
            lines = ["Agents (current native activity)"]
            for number, agent in enumerate(activity.agents, 1):
                parent = f" ← {agent.parent_id}" if agent.parent_id else ""
                lines.append(
                    f"{number}. {agent.id}{parent} · {agent.status} · {agent.model or 'model unavailable'}"
                )
                if agent.assignment:
                    lines.append(agent.assignment)
            await self._send_chunked(adapter, conversation, "\n".join(lines))
            self._agent_choices[choice_key] = (
                binding.session_id,
                tuple(agent.id for agent in activity.agents),
            )
            return
        verb, _, identifier = argument.partition(" ")
        explicit = identifier.startswith("id ")
        if explicit:
            identifier = identifier[3:].strip()
        if verb not in {"show", "background"} or not identifier or " " in identifier:
            await self._send_chunked(
                adapter, conversation, "Usage: /agents [show|background <n|id>]"
            )
            return
        if identifier.isdecimal() and not explicit:
            choices = self._agent_choices.get(choice_key)
            if (
                choices is None
                or choices[0] != binding.session_id
                or len(identifier) > len(str(len(choices[1])))
                or not 1 <= int(identifier) <= len(choices[1])
            ):
                await self._send_chunked(
                    adapter,
                    conversation,
                    "Agent number unavailable. Run /agents again.",
                )
                return
            identifier = choices[1][int(identifier) - 1]
        if verb == "background":
            outcome = await self._client.background_agent(
                binding.session_id, identifier
            )
            await self._send_chunked(
                adapter,
                conversation,
                f"Agent {identifier}: {outcome.replace('_', ' ')}",
            )
            return
        agent = next(
            (agent for agent in activity.agents if agent.id == identifier), None
        )
        if agent is None:
            await self._send_chunked(
                adapter, conversation, "Agent is no longer discoverable."
            )
            return
        lines = [
            f"Agent {agent.id}",
            f"State: {agent.status}",
            f"Model: {agent.model or 'unavailable'}",
        ]
        if agent.parent_id:
            lines.append(f"Parent: {agent.parent_id}")
        if agent.assignment:
            lines.append(f"Assignment: {agent.assignment}")
        if agent.task:
            lines.append(
                f"Task: {agent.task.id} · {'background' if agent.task.detached else 'foreground'}"
            )
        lines.extend(_todo_lines(agent.todos))
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _handle_todos(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        if argument not in {"", "all"}:
            await self._send_chunked(adapter, conversation, "Usage: /todos [all]")
            return
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        activity = await self._client.inspect_activity(binding.session_id)
        blocks = [
            f"Agent {agent.id}\n" + "\n".join(_todo_lines(agent.todos))
            for agent in activity.agents
            if argument == "all" or agent.id == "main"
        ]
        await self._send_chunked(
            adapter, conversation, "\n\n".join(blocks) or "No live agent todos."
        )


def _todo_lines(items: tuple[TodoItem, ...]) -> list[str]:
    counts = {
        status: sum(item.status == status for item in items)
        for status in ("done", "in_progress", "pending")
    }
    markers = {"done": "✓", "in_progress": "→", "pending": "○"}
    return [
        f"Todos: {counts['done']}/{len(items)} completed · {counts['in_progress']} in progress · {counts['pending']} pending",
        *(f"{markers[item.status]} {item.title}" for item in items),
    ]
