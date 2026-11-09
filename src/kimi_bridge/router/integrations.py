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
    "Native MCP runtime: the most recently created session, if it is live."
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
                "\n".join(
                    f"{item.id} · {item.name} · {'enabled' if item.enabled else 'disabled'} · {item.state} · {item.skill_count} skills · {item.server_count} MCP servers"
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
            f"{item.name} ({item.id})",
            f"Version: {item.version or 'unavailable'}",
            f"State: {item.state} · {'enabled' if item.enabled else 'disabled'}",
            "Declared skills: " + (", ".join(details.skills) or "none"),
        ]
        lines.extend(
            f"MCP: {server.name} → {server.runtime_name} · {server.transport} · {'enabled' if server.enabled else 'disabled'}"
            for server in details.servers
        )
        lines.append(
            "Diagnostics: "
            + (
                ", ".join(
                    f"{count} {severity}" for severity, count in details.diagnostics
                )
                or "none"
            )
        )
        lines.append(
            "Available tools in the bound session: "
            + (", ".join(details.tools) or "none")
        )
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
                f"{MCP_RUNTIME_SCOPE}\nRestart acknowledged for {server.id}. Use /mcp show {server.id} to inspect connection state.",
            )
            return
        inspection = await self._client.inspect_mcp(binding.session_id)
        lines = [
            MCP_RUNTIME_SCOPE,
            f"Authentication/configuration and available tools below use bound session {binding.session_id}.",
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
            f"{server.id} · {server.transport} · {server.status} · {server.tool_count} runtime tools"
            for server in servers
        )
        if verb == "servers" and not servers:
            lines.append("No servers in that native runtime.")
        names = {server.id for server in servers} | {server.name for server in servers}
        if verb == "show":
            names.add(name)
        lines.extend(
            f"Authentication: {server} · {status}"
            for server, status in inspection.auth_statuses
            if verb == "servers" or server in names
        )
        if verb == "show":
            lines.append(
                "Available bound-session tools: "
                + (
                    ", ".join(
                        tool.name
                        for tool in inspection.tools
                        if tool.mcp_server_id in names
                    )
                    or "none"
                )
            )
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
