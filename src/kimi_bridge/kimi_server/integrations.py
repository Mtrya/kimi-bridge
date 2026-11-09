"""Public native integration metadata and managed-account quota."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from datetime import datetime
from typing import Any

from .wire import _tool_info_from_wire
from .types import (
    AccountQuota,
    KimiServerAPIError,
    KimiServerOperationError,
    KimiServerProtocolError,
    KimiServerTransportError,
    McpInspection,
    McpServer,
    PluginDetails,
    PluginInfo,
    PluginServer,
    QuotaWindow,
    ToolInfo,
)


def _plugin(value: dict[str, Any]) -> PluginInfo:
    return PluginInfo(
        value["id"],
        value["displayName"],
        value["enabled"],
        value["state"],
        value.get("version"),
        int(value["skillCount"]),
        int(value["mcpServerCount"]),
    )


def _select(
    items: tuple[PluginInfo, ...] | tuple[McpServer, ...], selector: str
) -> PluginInfo | McpServer:
    matches = [item for item in items if item.id == selector]
    if not matches:
        matches = [
            item for item in items if item.name.casefold() == selector.casefold()
        ]
    if len(matches) != 1:
        raise KimiServerOperationError(
            "Integration name is unknown or ambiguous; use its native ID"
        )
    return matches[0]


def _mcp_tool_namespace(name: str) -> str:
    # Native qualified tool names sanitize server names, then collapse underscores.
    return re.sub(r"_+", "_", re.sub(r"[^a-zA-Z0-9_-]", "_", name))


def _tools_for_servers(
    tools: tuple[ToolInfo, ...], names: set[str]
) -> tuple[ToolInfo, ...]:
    namespaces = {_mcp_tool_namespace(name) for name in names}
    return tuple(
        tool for tool in tools
        if tool.active and tool.source == "mcp"
        and tool.mcp_server_id is not None
        and _mcp_tool_namespace(tool.mcp_server_id) in namespaces
    )


class IntegrationOperations:
    async def _integration_request(self, operation: str, **kwargs: Any):
        try:
            return await self._request_operation(operation, **kwargs)
        except KimiServerAPIError as exc:
            # Native integration errors can include token-bearing configuration.
            raise KimiServerOperationError(
                f"Native integration request failed (code {exc.code}). Inspect diagnostics in Kimi."
            ) from None
        except KimiServerTransportError:
            raise KimiServerOperationError(
                "Native integration request could not be read."
            ) from None

    async def _integration_tools(self, session_id: str) -> tuple[ToolInfo, ...]:
        data = await self._integration_request("list_tools", params={"session_id": session_id})
        return tuple(_tool_info_from_wire(item) for item in data["tools"])

    async def list_plugins(self) -> tuple[PluginInfo, ...]:
        data = await self._integration_request("list_plugins")
        return tuple(_plugin(value) for value in data["plugins"])

    async def get_plugin_details(self, session_id: str, selector: str) -> PluginDetails:
        selected = _select(await self.list_plugins(), selector)
        value, tools = await asyncio.gather(
            self._integration_request(
                "plugin_details", path_parameters={"plugin_id": selected.id}
            ),
            self._integration_tools(session_id),
        )
        servers = tuple(
            PluginServer(
                item["name"], item["runtimeName"], item["enabled"], item["transport"]
            )
            for item in value["mcpServers"]
        )
        runtime_names = {item.runtime_name for item in servers}
        return PluginDetails(
            _plugin(value),
            tuple(item["name"] for item in value["skills"]),
            servers,
            tuple(
                sorted(
                    Counter(item["severity"] for item in value["diagnostics"]).items()
                )
            ),
            tuple(
                tool.name
                for tool in _tools_for_servers(tools, runtime_names)
            ),
        )

    async def list_mcp_servers(self) -> tuple[McpServer, ...]:
        data = await self._integration_request("mcp_servers")
        return tuple(
            McpServer(
                item["id"],
                item["name"],
                item["transport"],
                item["status"],
                item["tool_count"],
            )
            for item in data["servers"]
        )

    async def inspect_mcp(
        self, session_id: str, *, selector: str | None = None
    ) -> McpInspection:
        session = await self.get_session(session_id)
        servers, auth, tools = await asyncio.gather(
            self.list_mcp_servers(),
            self._integration_request(
                "mcp_auth_statuses",
                params={"cwd": session["metadata"]["cwd"], "verify": "false"},
            ),
            self._integration_tools(session_id),
        )
        inspection = McpInspection(
            servers,
            tuple((item["name"], item["authStatus"]) for item in auth),
            tuple(tool for tool in tools if tool.active and tool.source == "mcp"),
        )
        if selector is None:
            return inspection
        namespace = _mcp_tool_namespace(selector)
        selected = tuple(
            server for server in servers
            if namespace in {
                _mcp_tool_namespace(server.id), _mcp_tool_namespace(server.name)
            }
        )
        names = {selector}
        names.update(name for server in selected for name in (server.id, server.name))
        namespaces = {_mcp_tool_namespace(name) for name in names}
        return McpInspection(
            selected,
            tuple(
                (name, status) for name, status in inspection.auth_statuses
                if _mcp_tool_namespace(name) in namespaces
            ),
            _tools_for_servers(inspection.tools, names),
        )

    async def restart_mcp_server(self, selector: str) -> McpServer:
        selected = _select(await self.list_mcp_servers(), selector)
        result = await self._integration_request(
            "restart_mcp", path_parameters={"server_id": selected.id}
        )
        if result.get("restarting") is not True:
            raise KimiServerProtocolError("MCP restart returned no acknowledgement")
        return selected

    async def get_account_quota(self) -> AccountQuota:
        data = await self._integration_request("account_quota")
        if data["kind"] == "error":
            return AccountQuota(False)
        if data["kind"] != "ok":
            raise KimiServerProtocolError("unknown account quota result")
        usages = data["quota"]["usages"]
        windows = []
        for key, name in (
            ("limit5h", "5 hours"),
            ("limit7d", "7 days"),
            ("monthTotal", "Month total"),
            ("monthCode", "Month code"),
        ):
            value = usages.get(key)
            if value is None:
                continue
            reset_at = value.get("resetAt")
            if reset_at is not None:
                try:
                    reset_at = datetime.fromisoformat(reset_at.replace("Z", "+00:00"))
                except ValueError:
                    reset_at = None
            windows.append(QuotaWindow(name, float(value["usedRatio"]), reset_at))
        return AccountQuota(True, tuple(windows))
