"""On-demand integration inspection and account quota presentation."""

from __future__ import annotations

import logging
import math
from datetime import timezone

from ..kimi_server import AccountQuota, KimiServerOperationError
from ..platforms.base import ConversationRef, PlatformAdapter
from .formatting import _usage_bar


LOGGER = logging.getLogger(__name__)
MCP_RUNTIME_SCOPE = (
    "Connections use the most recently created session, if live."
)


class _IntegrationMixin:
    async def _handle_plugins(
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
            plugins = await self._client.list_plugins()
            text = (
                "\n\n".join(
                    f"**{item.name}** · {'enabled' if item.enabled else 'disabled'} · {item.state}\n"
                    f"{item.skill_count} skills · {item.server_count} MCP servers\n`{item.id}`"
                    for item in plugins
                )
                or "No installed Kimi plugins."
            )
            await self._send_chunked(adapter, conversation, text)
            return
        verb, _, name = argument.partition(" ")
        if verb != "show" or not name.strip():
            await self._send_chunked(
                adapter, conversation, "Usage: /plugins [show <name>]"
            )
            return
        details = await self._client.get_plugin_details(
            binding.session_id, name.strip()
        )
        item = details.plugin
        lines = [
            f"**{item.name}** · {'enabled' if item.enabled else 'disabled'} · {item.state}",
            f"Version: {item.version or 'unavailable'} · ID: {item.id}",
            "",
            "**Skills**",
            "\n".join(f"- {skill}" for skill in details.skills) or "None",
            "",
            "**MCP servers**",
        ]
        lines.extend(
            f"- **{server.name}** · {server.transport} · {'enabled' if server.enabled else 'disabled'}\n  Runtime: {server.runtime_name}"
            for server in details.servers
        )
        if not details.servers:
            lines.append("None")
        diagnostics = ", ".join(f"{count} {severity}" for severity, count in details.diagnostics)
        lines.extend(("", "**Diagnostics**", diagnostics or "None"))
        lines.extend(("", "**Available tools · current session**", "\n".join(f"- {tool}" for tool in details.tools) or "None"))
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _handle_mcp_inspection(
        self,
        conversation_key: str,
        adapter: PlatformAdapter,
        conversation: ConversationRef,
        argument: str,
    ) -> None:
        binding = await self._require_binding(conversation_key, adapter, conversation)
        if binding is None:
            return
        verb, _, name = argument.partition(" ")
        name = name.strip()
        if (
            verb not in {"servers", "show", "restart"}
            or (verb == "servers" and name)
            or (verb in {"show", "restart"} and not name)
        ):
            await self._send_chunked(
                adapter,
                conversation,
                "Usage: /mcp [servers|show <server>|restart <server>]",
            )
            return
        if verb == "restart":
            server = await self._client.restart_mcp_server(name)
            await self._send_chunked(
                adapter,
                conversation,
                f"✓ **Restart acknowledged** · {server.id}\n{MCP_RUNTIME_SCOPE}\nCheck with /mcp show {server.id}.",
            )
            return
        inspection = await self._client.inspect_mcp(binding.session_id)
        lines = [
            "**Connections**",
            MCP_RUNTIME_SCOPE,
        ]
        servers = (
            inspection.servers
            if verb == "servers"
            else tuple(
                server
                for server in inspection.servers
                if name in {server.id, server.name}
            )
        )
        auth = dict(inspection.auth_statuses)
        matching_tools = tuple(tool for tool in inspection.tools if tool.mcp_server_id == name)
        if verb == "show" and not servers and name not in auth and not matching_tools:
            await self._send_chunked(
                adapter,
                conversation,
                "MCP server is unavailable in the native runtime/configuration.",
            )
            return
        lines.extend(
            f"\n**{server.id}** · {server.status}\n{server.transport} · {server.tool_count} runtime tools"
            for server in servers
        )
        if verb == "servers" and not servers:
            lines.append("No servers in that native runtime.")
        names = {server.id for server in servers} | {server.name for server in servers}
        if verb == "show":
            names.add(name)
        lines.extend(("", "**Authentication · current session**"))
        auth_lines = [
            f"- {server} · {status}"
            for server, status in inspection.auth_statuses
            if verb == "servers" or server in names
        ]
        lines.extend(auth_lines or ["No authentication state reported."])
        if verb == "show":
            lines.extend(("", "**Available tools · current session**",
                (
                    "\n".join(
                        f"- {tool.name}"
                        for tool in inspection.tools
                        if tool.mcp_server_id in names
                    )
                    or "none"
                ),
            ))
        await self._send_chunked(adapter, conversation, "\n".join(lines))

    async def _account_quota_text(self) -> str:
        try:
            quota = await self._client.get_account_quota()
        except (KimiServerOperationError, TimeoutError):
            LOGGER.warning("account quota could not be read", exc_info=True)
            return "Managed account quota\nUnavailable: account lookup failed."
        return _format_account_quota(quota)


def _format_account_quota(quota: AccountQuota) -> str:
    if not quota.available:
        return "Managed account quota\nUnavailable for this provider/account."
    lines = ["Managed account quota"]
    if not quota.windows:
        lines.append("No quota windows reported.")
    for window in quota.windows:
        ratio = (
            max(0, min(1, window.used_ratio)) if math.isfinite(window.used_ratio) else 0
        )
        text = f"{window.name}: {_usage_bar(ratio)} {ratio * 100:.1f}% used"
        if window.reset_at is not None:
            reset = window.reset_at
            label = (
                reset.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                if reset.tzinfo is not None
                else reset.isoformat(sep=" ") + " (time zone unspecified)"
            )
            text += f" · resets {label}"
        lines.append(text)
    return "\n".join(lines)
