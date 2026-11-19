"""Public native integration metadata and managed-account quota."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Mapping
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
        value.get("displayName", value.get("display_name")),
        value["enabled"],
        value["state"],
        value.get("version"),
        int(value.get("skillCount", value.get("skill_count"))),
        int(value.get("mcpServerCount", value.get("mcp_server_count"))),
    )


def _mcp_runtime_server(
    value: dict[str, Any], configured: Mapping[str, Any], tools: tuple[ToolInfo, ...]
) -> McpServer:
    locator = value.get("locator") or {
        "source": "global", "name": value.get("id", value.get("name")),
    }
    if locator.get("source") == "plugin":
        configured_name = f"{locator['plugin_id']}:{locator['server_name']}"
    else:
        configured_name = locator["name"]
    config = value.get("config")
    if not isinstance(config, dict):
        config = configured.get(configured_name)
    if not isinstance(config, dict):
        raise KimiServerProtocolError("MCP runtime omitted its configuration")
    server_id = str(value.get("server_id", configured_name))
    name = str(value.get("runtime_name", configured_name))
    auth = value.get("auth_status", value.get("authStatus"))
    if auth is None:
        auth = config.get("status", "connected")
    status = config.get("status", "connected")
    if (
        auth in {"oauth-required", "oauth-expired", "unavailable"}
        or status == "error"
        or value.get("error")
    ):
        status = "error"
    return McpServer(
        server_id,
        name,
        config["transport"],
        status,
        len(_tools_for_servers(tools, {server_id, name})),
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
                item["name"],
                item.get("runtimeName", item.get("runtime_name")),
                item["enabled"],
                item["transport"],
            )
            for item in value.get("mcpServers", value.get("mcp_servers", ()))
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

    async def _mcp_configuration(self) -> dict[str, Any]:
        data = await self._integration_request("mcp_servers")
        if isinstance(data, dict):
            data = data.get("servers", data.get("items"))
        if not isinstance(data, list):
            raise KimiServerProtocolError("MCP server configuration is not a list")
        configured = {}
        for value in data:
            name = str(value["name"])
            config = value.get("config", value)
            configured[name] = config if isinstance(config, dict) else value
        return configured

    async def _mcp_runtime_items(self, cwd: str | None = None) -> list[dict[str, Any]]:
        data = await self._integration_request(
            "mcp_inspect", json_body={"cwd": cwd} if cwd is not None else {}
        )
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("items"), list):
            return data["items"]
        raise KimiServerProtocolError("MCP inspection response is not a server list")

    async def list_mcp_servers(self) -> tuple[McpServer, ...]:
        configured, runtime = await asyncio.gather(
            self._mcp_configuration(), self._mcp_runtime_items()
        )
        return tuple(
            _mcp_runtime_server(item, configured, ()) for item in runtime
        )

    async def inspect_mcp(
        self, session_id: str, *, selector: str | None = None
    ) -> McpInspection:
        session = await self.get_session(session_id)
        configured, runtime, tools = await asyncio.gather(
            self._mcp_configuration(),
            self._mcp_runtime_items(session["metadata"]["cwd"]),
            self._integration_tools(session_id),
        )
        servers = tuple(
            _mcp_runtime_server(item, configured, tools) for item in runtime
        )
        inspection = McpInspection(
            servers,
            tuple(
                (
                    server.name,
                    value.get(
                        "auth_status", value.get("authStatus", server.status)
                    ),
                )
                for server, value in zip(servers, runtime)
            ),
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
        configured, runtime = await asyncio.gather(
            self._mcp_configuration(), self._mcp_runtime_items()
        )
        selected = _select(
            tuple(_mcp_runtime_server(item, configured, ()) for item in runtime),
            selector,
        )
        value = next(
            (
                item
                for item in runtime
                if str(item.get("server_id", item.get("id", ""))) == selected.id
            ),
            None,
        )
        if value is None:
            raise KimiServerProtocolError("MCP restart lost its selected server")
        locator = value.get("locator") or {
            "source": "global", "name": value.get("id", value.get("name")),
        }
        editable = value.get("editable")
        if editable is None:
            editable = locator.get("source") == "global"
        if not editable or locator.get("source") != "global":
            raise KimiServerOperationError("The selected MCP server is not editable")
        configured_value = configured.get(locator["name"])
        if not isinstance(configured_value, dict):
            configured_value = value.get("config")
        if not isinstance(configured_value, dict):
            raise KimiServerProtocolError("MCP restart omitted its configuration")
        result = await self._integration_request(
            "restart_mcp",
            path_parameters={"server_id": locator["name"]},
            json_body=configured_value,
        )
        if not isinstance(result, list):
            raise KimiServerProtocolError("MCP update returned no server list")
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
