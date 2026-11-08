from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import pytest

from kimi_bridge.kimi_server import (
    AccountQuota,
    KimiServerClient,
    KimiServerOperationError,
    McpInspection,
    McpServer,
    PluginDetails,
    PluginInfo,
    PluginServer,
    QuotaWindow,
    ToolInfo,
)
from kimi_bridge.router.integrations import _format_account_quota
from test_kimi_server import FakeHttpClient, _envelope
from test_router import FakeAdapter, FakeKimiClient, _control_session, _message
from test_session_controls import _router


def _plugin(enabled=True, state="ok"):
    return {
        "id": "weather",
        "displayName": "Weather",
        "enabled": enabled,
        "state": state,
        "skillCount": 1,
        "mcpServerCount": 1,
    }


async def test_plugin_projection_keeps_contributions_and_excludes_configuration_secrets():
    secret = "PRIVATE_TOKEN_SENTINEL"
    detail = {
        **_plugin(),
        "skills": [{"name": "forecast"}],
        "mcpServers": [
            {
                "name": "calendar",
                "runtimeName": "plugin-weather-calendar",
                "enabled": True,
                "transport": "http",
                "url": "https://example.invalid/?token=" + secret,
                "env": {"TOKEN": secret},
                "headers": {"Authorization": secret},
            }
        ],
        "diagnostics": [{"severity": "error", "message": secret}],
        "originalSource": secret,
    }
    tools = {
        "tools": [
            {
                "name": "available",
                "description": "",
                "source": "mcp",
                "mcp_server_id": "plugin-weather-calendar",
                "active": True,
            },
            {
                "name": "inactive",
                "description": "",
                "source": "mcp",
                "mcp_server_id": "plugin-weather-calendar",
                "active": False,
            },
        ]
    }
    http = FakeHttpClient(
        [_envelope({"plugins": [_plugin()]}), _envelope(detail), _envelope(tools)]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        result = await client.get_plugin_details("session-1", "Weather")
    assert result.skills == ("forecast",) and result.tools == ("available",)
    assert result.diagnostics == (("error", 1),)
    assert result.servers[0].runtime_name == "plugin-weather-calendar"
    assert secret not in repr(asdict(result))
    assert all(request[0] == "GET" for request in http.requests)


async def test_unknown_plugin_name_does_not_mutate_or_fetch_details():
    http = FakeHttpClient([_envelope({"plugins": [_plugin()]})])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerOperationError):
            await client.get_plugin_details("session-1", "unknown")
    assert len(http.requests) == 1


async def test_mcp_inspection_keeps_runtime_auth_and_bound_tools_scopes_separate():
    http = FakeHttpClient(
        [
            _envelope({"metadata": {"cwd": "/bound"}}),
            _envelope(
                {
                    "servers": [
                        {
                            "id": "server",
                            "name": "server",
                            "transport": "http",
                            "status": "error",
                            "tool_count": 0,
                            "last_error": "PRIVATE_TOKEN_SENTINEL",
                        }
                    ]
                }
            ),
            _envelope([{"name": "server", "authStatus": "oauth-required"}]),
            _envelope(
                {
                    "tools": [
                        {
                            "name": "bound_tool",
                            "description": "",
                            "source": "mcp",
                            "mcp_server_id": "server",
                            "active": True,
                        }
                    ]
                }
            ),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        result = await client.inspect_mcp("bound-session")
    assert result.servers[0].status == "error"
    assert result.auth_statuses == (("server", "oauth-required"),)
    assert result.tools[0].name == "bound_tool"
    assert "PRIVATE_TOKEN_SENTINEL" not in repr(result)
    assert http.requests[1][1].endswith("/api/v1/mcp/servers")
    assert http.requests[2][1].endswith("/api/v2/mcp/auth-statuses")
    assert http.requests[2][2]["params"] == {"cwd": "/bound", "verify": "false"}
    assert http.requests[3][2]["params"] == {"session_id": "bound-session"}
    assert all(request[0] == "GET" for request in http.requests)


@pytest.mark.parametrize("status", ["connected", "connecting", "disconnected", "error"])
async def test_mcp_restart_only_targets_selected_server_and_does_not_claim_health(
    status,
):
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "servers": [
                        {
                            "id": "selected/server",
                            "name": "Selected",
                            "transport": "stdio",
                            "status": status,
                            "tool_count": 2,
                        }
                    ]
                }
            ),
            _envelope({"restarting": True}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        server = await client.restart_mcp_server("Selected")
    assert server.status == status
    assert http.requests[1][0] == "POST" and http.requests[1][1].endswith(
        "/mcp/servers/selected%2Fserver:restart"
    )
    assert len(http.requests) == 2


async def test_native_integration_error_does_not_forward_raw_diagnostics():
    http = FakeHttpClient([_envelope(None, code=50000, msg="PRIVATE_TOKEN_SENTINEL")])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerOperationError) as error:
            await client.list_plugins()
    assert "PRIVATE_TOKEN_SENTINEL" not in str(error.value)
    assert error.value.__suppress_context__


async def test_quota_projection_percentages_optional_windows_and_timezone():
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "kind": "ok",
                    "quota": {
                        "usages": {
                            "limit5h": {
                                "usedRatio": 0.125,
                                "resetAt": "2026-11-09T08:00:00+08:00",
                            },
                            "monthCode": {"usedRatio": 0.25},
                        },
                        "extraUsage": {"PRIVATE_TOKEN_SENTINEL": True},
                    },
                }
            )
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        result = await client.get_account_quota()
    assert [window.name for window in result.windows] == ["5 hours", "Month code"]
    assert result.windows[0].used_ratio == 0.125
    text = _format_account_quota(result)
    assert "12.5%" in text and "25.0%" in text and "2026-11-09 00:00:00 UTC" in text
    assert "PRIVATE_TOKEN_SENTINEL" not in repr(result)
    assert http.requests[0][0] == "GET"


async def test_quota_in_band_provider_error_is_unavailable():
    http = FakeHttpClient(
        [
            _envelope(
                {"kind": "error", "message": "PRIVATE_TOKEN_SENTINEL", "status": 403}
            )
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        assert await client.get_account_quota() == AccountQuota(False)


class IntegrationClient(FakeKimiClient):
    def __init__(self, quota_error=False):
        super().__init__()
        self.sessions = [_control_session()]
        self.quota_error = quota_error
        self.restarted = []

    async def get_account_quota(self):
        if self.quota_error:
            raise KimiServerOperationError("fixture quota failure")
        return AccountQuota(
            True,
            (QuotaWindow("5 hours", 0.4, datetime(2026, 11, 9, tzinfo=timezone.utc)),),
        )

    async def list_plugins(self):
        return (PluginInfo("fixture", "Fixture", False, "error", skill_count=1),)

    async def get_plugin_details(self, session_id, selector):
        return PluginDetails(
            (await self.list_plugins())[0],
            ("skill",),
            (PluginServer("server", "server", False, "http"),),
            (("error", 1),),
            (),
        )

    async def inspect_mcp(self, session_id):
        return McpInspection(
            (McpServer("server", "server", "http", "disconnected", 0),),
            (("server", "oauth-required"),),
            (ToolInfo("available", "", "mcp", "server"),),
        )

    async def restart_mcp_server(self, selector):
        self.restarted.append(selector)
        return McpServer("server", "server", "http", "disconnected", 0)


@pytest.mark.parametrize("quota_error", [True, False])
async def test_usage_preserves_session_statistics_on_account_only_failure(
    tmp_path: Path, quota_error
):
    client = IntegrationClient(quota_error)
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter(message_limit=4000)
    try:
        await router.handle_inbound(adapter, _message("/usage"))
    finally:
        await router.close()
    text = adapter.sent[-1][2]
    assert "Input tokens: 10" in text and "Output tokens: 20" in text
    assert "Managed account quota" in text
    assert (
        ("Unavailable" in text) if quota_error else ("40.0%" in text and "UTC" in text)
    )
    assert client.prompts == []


async def test_plugin_and_mcp_commands_remain_on_demand_and_restart_acknowledged(
    tmp_path: Path,
):
    client = IntegrationClient()
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter(message_limit=4000)
    try:
        for command in [
            "/plugins",
            "/plugins show Fixture",
            "/mcp servers",
            "/mcp show server",
            "/mcp restart server",
        ]:
            await router.handle_inbound(adapter, _message(command))
    finally:
        await router.close()
    assert any("disabled" in text and "error" in text for _, _, text in adapter.sent)
    assert any(
        "oauth-required" in text and "available" in text for _, _, text in adapter.sent
    )
    assert (
        "Restart acknowledged" in adapter.sent[-1][2]
        and "connected" not in adapter.sent[-1][2]
    )
    assert client.restarted == ["server"] and not client.prompts


@pytest.mark.parametrize("operation", ["plugin", "mcp"])
async def test_integration_tool_errors_never_expose_native_diagnostics(operation):
    secret = "PRIVATE_TOOL_DIAGNOSTIC"
    responses = (
        [
            _envelope({"plugins": [_plugin()]}),
            _envelope({**_plugin(), "skills": [], "mcpServers": [], "diagnostics": []}),
        ]
        if operation == "plugin"
        else [
            _envelope({"metadata": {"cwd": "/bound"}}),
            _envelope({"servers": []}),
            _envelope([]),
        ]
    )
    http = FakeHttpClient([*responses, _envelope(None, code=50000, msg=secret)])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerOperationError) as error:
            if operation == "plugin":
                await client.get_plugin_details("session-1", "weather")
            else:
                await client.inspect_mcp("session-1")
    import traceback

    assert secret not in "".join(traceback.format_exception(error.value))


async def test_mcp_show_retains_bound_tools_without_runtime_or_auth_entry(tmp_path):
    class Client(IntegrationClient):
        async def inspect_mcp(self, session_id):
            return McpInspection(
                (), (), (ToolInfo("fixture_tool", "", "mcp", "tool-only"),)
            )

    router, _ = _router(tmp_path, Client())
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/mcp show tool-only"))
        assert any("fixture_tool" in text for _, _, text in adapter.sent)
    finally:
        await router.close()
