from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from kimi_bridge.kimi_server import (
    FileContext,
    KimiServerClient,
    PromptContent,
    PromptMedia,
    RecordedTurn,
    WorkspaceInfo,
)
from kimi_bridge.platforms.base import InboundFile, InboundImage
from kimi_bridge.router.workspace import _parse_new_options
from test_kimi_server import FakeHttpClient, _envelope
from test_kimi_server import _profile_payload
from test_router import (
    FakeAdapter,
    FakeKimiClient,
    _approval,
    _control_session,
    _message,
)
from test_session_controls import _router
from test_workspace_files import FileServer
from test_workspace_files import _setup


def _change(status="modified", **fields):
    return {
        "path": "fixture.txt",
        "status": status,
        "additions": 1,
        "deletions": 1,
        **fields,
    }


def _context_responses(cwd="/target"):
    return [
        _envelope({"metadata": {"cwd": "/host"}}),
        _envelope(
            {"workspace_id": "workspace", "environment_id": "remote", "cwd": cwd}
        ),
        _envelope(
            {"items": [{"id": "workspace", "root": "/host", "additional_dirs": []}]}
        ),
    ]


async def test_latest_changes_select_finished_turn_with_assistant_steps():
    transcript = {
        "items": [
            {"kind": "turn", "ordinal": 2, "state": "completed", "steps": [{}]},
            {"kind": "turn", "ordinal": 3, "state": "failed", "steps": [{}]},
            {"kind": "turn", "ordinal": 4, "state": "cancelled", "steps": []},
            {"kind": "turn", "ordinal": 5, "state": "running", "steps": [{}]},
        ]
    }
    http = FakeHttpClient(
        [_envelope({"recorded": True, "changes": [_change()]})],
        transcript_snapshots=[transcript],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        changes = await client.get_file_changes("session-1")
    assert changes.turn == RecordedTurn(3)
    assert changes.changes[0].additions == 1
    assert http.requests[1][2]["params"] == {"turn_id": 3}


@pytest.mark.parametrize(
    "status,before,after",
    [
        (
            "modified",
            {"version": 1, "content": "before\n"},
            {"version": 2, "content": "after\n"},
        ),
        ("added", {"version": 1}, {"version": 2, "content": "new\n"}),
        ("deleted", {"version": 1, "content": "deleted\n"}, {"version": 2}),
    ],
)
async def test_recorded_diff_uses_actual_start_end_content_without_current_files(
    status, before, after
):
    http = FakeHttpClient(
        [
            _envelope({"recorded": True, "changes": [_change(status)]}),
            *_context_responses(),
            _envelope({"content": before}),
            _envelope({"content": after}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff(
            "session-1", "/target/fixture.txt", turn=RecordedTurn(7)
        )
    assert diff.unavailable is None and not diff.truncated
    assert (
        ("-before" in diff.text and "+after" in diff.text)
        if status == "modified"
        else ("+new" in diff.text if status == "added" else "-deleted" in diff.text)
    )
    content_requests = [
        request
        for request in http.requests
        if request[1].endswith("/file-history/content")
    ]
    assert [request[2]["params"] for request in content_requests] == [
        {"turn_id": 7, "path": "fixture.txt", "phase": phase}
        for phase in ("start", "end")
    ]
    assert not any("/fs:" in request[1] for request in http.requests)


@pytest.mark.parametrize(
    "recorded,change,before,reason",
    [
        (False, _change(), None, "unrecorded"),
        (True, _change(binary=True), None, "binary"),
        (True, _change(oversize=True), None, "oversize"),
        (True, _change(), None, "missing"),
    ],
)
async def test_unavailable_recordings_are_not_empty_diffs(
    recorded, change, before, reason
):
    responses = [_envelope({"recorded": recorded, "changes": [change]})]
    if recorded:
        responses.extend(_context_responses())
    if reason == "missing":
        responses.extend(
            [
                _envelope({"content": before}),
                _envelope({"content": {"version": 2, "content": "after"}}),
            ]
        )
    http = FakeHttpClient(responses)
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff(
            "session-1", "fixture.txt", turn=RecordedTurn(7)
        )
    assert diff.text is None and diff.unavailable == reason


async def test_diff_preview_is_bounded():
    http = FakeHttpClient(
        [
            _envelope({"recorded": True, "changes": [_change()]}),
            *_context_responses(),
            _envelope({"content": {"version": 1, "content": "a" * 20000 + "\n"}}),
            _envelope({"content": {"version": 2, "content": "b" * 20000 + "\n"}}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff(
            "session-1", "fixture.txt", turn=RecordedTurn(7)
        )
    assert diff.truncated and len(diff.text) == 8192


@pytest.mark.parametrize(
    "argument,expected",
    [
        (r"C:\My Workspace", (r"C:\My Workspace", None, None)),
        (
            '"/host with spaces" --env remote --cwd "/target with spaces"',
            ("/host with spaces", "remote", "/target with spaces"),
        ),
        ("--env remote", ("", "remote", None)),
        ('"/host --env literal" --env remote', ("/host --env literal", "remote", None)),
        (r"C:\host --env remote --cwd C:\target", (r"C:\host", "remote", r"C:\target")),
    ],
)
def test_new_environment_parser_keeps_path_namespaces(argument, expected):
    assert _parse_new_options(argument) == expected


@pytest.mark.parametrize("argument", ["--env", "--env a --env b", "--cwd /target"])
def test_new_environment_parser_rejects_missing_or_duplicate_flags(argument):
    with pytest.raises(ValueError):
        _parse_new_options(argument)


async def test_native_creation_keeps_environment_cwd_out_of_host_path_resolution(
    tmp_path: Path,
):
    http = FakeHttpClient([_envelope({"id": "created"}), _envelope(_profile_payload())])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        await client.create_session(
            str(tmp_path),
            environment_id="remote",
            environment_cwd="/target path",
            model="kimi-code/k3",
            swarm_mode=False,
        )
    assert http.requests[0][2]["json"] == {
        "metadata": {"cwd": str(tmp_path)},
        "environment_id": "remote",
        "environment_cwd": "/target path",
    }
    assert http.requests[1][2]["json"]["agent_config"] == {
        "model": "kimi-code/k3",
        "swarm_mode": False,
    }


async def test_remote_binding_without_cwd_fails_instead_of_using_host(tmp_path: Path):
    http = FakeHttpClient(
        [
            _envelope({"metadata": {"cwd": str(tmp_path)}}),
            _envelope({"workspace_id": "workspace", "environment_id": "remote"}),
            _envelope(
                {
                    "items": [
                        {
                            "id": "workspace",
                            "root": str(tmp_path),
                            "additional_dirs": [],
                        }
                    ]
                }
            ),
        ]
    )
    from kimi_bridge.kimi_server import KimiServerProtocolError

    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerProtocolError):
            await client.get_file_context("session-1")


async def test_target_failure_does_not_download_host_file(tmp_path: Path):
    (tmp_path / "fixture.txt").write_text("HOST")
    http = FakeHttpClient(
        [
            *_context_responses(str(tmp_path)),
            _envelope(None, code=40926, msg="target disconnected"),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        from kimi_bridge.kimi_server import KimiServerAPIError

        with pytest.raises(KimiServerAPIError) as error:
            await client.download_workspace_file("session-1", "fixture.txt")
    assert error.value.code == 40926
    assert http.requests[-1][1].endswith("/sessions/session-1/fs/fixture.txt:download")


async def test_remote_inbound_files_and_media_fallback_never_write_host_inbox(
    tmp_path: Path,
):
    host = tmp_path / "host"
    host.mkdir()
    client = FakeKimiClient()
    client.sessions = [{**_control_session(), "metadata": {"cwd": str(host)}}]
    client.file_contexts["session-control"] = FileContext(
        WorkspaceInfo("workspace", str(host), ()), str(host), "remote"
    )
    client.session_model = replace(client.session_model, capabilities=())
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(
            adapter,
            _message(
                "inspect",
                files=(InboundFile(b"remote-file", "fixture.txt", "text/plain"),),
                images=(InboundImage(b"remote-image", "image/png", "fixture.png"),),
            ),
        )
    finally:
        await router.close()
    content = client.prompts[0][1]
    assert isinstance(content, PromptContent)
    assert [(item.kind, item.data) for item in content.media] == [
        ("file", b"remote-file"),
        ("file", b"remote-image"),
    ]
    assert list(host.iterdir()) == []
    assert "saved at" not in content.text


async def test_generic_file_prompt_upload_uses_native_file_reference():
    http = FakeHttpClient(
        [
            _envelope({"id": "uploaded"}),
            _envelope({"prompt_id": "prompt-1", "status": "running"}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        await client.submit_prompt(
            "session-1",
            PromptContent(
                media=(PromptMedia("file", b"target", "fixture.txt", "text/plain"),)
            ),
        )
    assert http.requests[1][2]["json"]["content"] == [
        {
            "type": "file",
            "file_id": "uploaded",
            "name": "fixture.txt",
            "media_type": "text/plain",
            "size": 6,
        }
    ]


async def test_plan_document_uses_native_agent_projection_and_never_wire_host_path(
    tmp_path: Path,
):
    host = tmp_path / "plan.md"
    host.write_text("HOST SECRET")
    request = replace(
        _approval(),
        tool_name="ExitPlanMode",
        agent_id="child",
        tool_call_id="plan-tool",
        input_display={"path": str(host)},
    )
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "plans": [
                        {"tool_call_id": "unrelated", "plan": "WRONG"},
                        {
                            "tool_call_id": "plan-tool",
                            "plan": "TARGET PLAN",
                            "path": str(host),
                        },
                    ]
                }
            )
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        document = await client.get_plan_document(request)
    assert document.text == "TARGET PLAN" and document.name == "plan.md"
    assert http.requests[0][2]["params"] == {
        "agent_id": "child",
        "tool_call_id": "plan-tool",
    }
    assert "HOST SECRET" not in document.text


async def test_target_file_reads_are_scoped_to_native_session_without_host_fallback(
    tmp_path: Path,
):
    host = tmp_path / "same-path"
    host.mkdir()
    (host / "fixture.txt").write_text("HOST")

    class TargetServer(FileServer):
        async def request(self, method, url, **kwargs):
            path = httpx.URL(url).path
            request = httpx.Request(method, url)
            if path.endswith("/environment"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "workspace_id": "workspace-fixture",
                            "environment_id": "remote",
                            "cwd": str(host),
                        },
                    },
                    request=request,
                )
            if path.endswith("fs:read"):
                return httpx.Response(
                    200,
                    json={
                        "code": 0,
                        "data": {
                            "path": "fixture.txt",
                            "content": "TARGET",
                            "encoding": "utf-8",
                            "size": 6,
                            "is_binary": False,
                            "truncated": False,
                        },
                    },
                    request=request,
                )
            if path.endswith(":download"):
                return httpx.Response(
                    200,
                    content=b"TARGET",
                    headers={
                        "content-disposition": "attachment",
                        "content-type": "text/plain",
                    },
                    request=request,
                )
            return await super().request(method, url, **kwargs)

    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=TargetServer(host)
    ) as client:
        assert (
            await client.preview_workspace_file("session-1", "fixture.txt")
        ).text == "TARGET"
        assert (
            await client.download_workspace_file("session-1", "fixture.txt")
        ).data == b"TARGET"
    assert (host / "fixture.txt").read_text() == "HOST"


async def test_changes_selection_keeps_turn_then_is_replaced_by_file_list(
    tmp_path: Path,
):
    workspace, client, _, adapter, router = _setup(tmp_path)
    (workspace / "current.txt").write_text("current")

    class HistoryServer(FileServer):
        latest = 7
        content_turns = []

        async def request(self, method, url, **kwargs):
            path = httpx.URL(url).path
            request = httpx.Request(method, url)
            data = None
            if path.endswith("/transcript"):
                data = {
                    "items": [
                        {
                            "kind": "turn",
                            "ordinal": self.latest,
                            "state": "completed",
                            "steps": [{}],
                        }
                    ]
                }
            elif path.endswith("/file-history/changes"):
                data = {
                    "recorded": True,
                    "changes": [_change("deleted", path="deleted.txt")]
                    if kwargs["params"]["turn_id"] == 7
                    else [],
                }
            elif path.endswith("/file-history/content"):
                self.content_turns.append(kwargs["params"]["turn_id"])
                data = {
                    "content": {"version": 1, "content": "deleted fixture\n"}
                    if kwargs["params"]["phase"] == "start"
                    else {"version": 2}
                }
            if data is not None:
                return httpx.Response(
                    200, json={"code": 0, "data": data}, request=request
                )
            return await super().request(method, url, **kwargs)

    server = HistoryServer(workspace)
    client.server = server
    client.native._http = server
    client.get_file_changes = client.native.get_file_changes
    client.get_file_diff = client.native.get_file_diff
    try:
        await router.handle_inbound(adapter, _message("/files changes"))
        server.latest = 8
        await router.handle_inbound(adapter, _message("/files diff 1"))
        assert "-deleted fixture" in adapter.sent[-1][2]
        assert server.content_turns == [7, 7]
        await router.handle_inbound(adapter, _message("/files"))
        await router.handle_inbound(adapter, _message("/files diff 1"))
        assert server.content_turns == [7, 7]
        assert "missing" in adapter.sent[-1][2]
    finally:
        await router.close()
