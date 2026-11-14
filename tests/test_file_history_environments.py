from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from kimi_bridge.kimi_server import (
    FileContext,
    KimiServerClient,
    KimiServerOperationError,
    PromptContent,
    PromptMedia,
    RecordedTurn,
    WorkspaceInfo,
)
from kimi_bridge.platforms.base import InboundFile, InboundImage
from kimi_bridge.router.workspace import _parse_new_options
from test_kimi_server import FakeHttpClient, _envelope
from test_kimi_server import _profile_payload
from test_kimi_turns import _turn
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


def _plan_response(plan: str, path: str):
    return _envelope(
        {
            "turns": [
                _turn(
                    "plan-turn",
                    items=[
                        {
                            "type": "tool_call",
                            "item_id": "plan-tool",
                            "status": "running",
                            "created_at": "now",
                            "display": {
                                "kind": "plan_review",
                                "plan": plan,
                                "path": path,
                            },
                        }
                    ],
                )
            ],
            "has_more": False,
        }
    )


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
    turns = [
        _turn(
            f"turn-{ordinal}",
            ordinal=ordinal,
            status="ended" if outcome else "running",
            outcome=outcome,
            turn_number=number,
            steps=[{}] if steps else [],
        )
        for ordinal, outcome, number, steps in (
            (2, "completed", 12, True),
            (3, "failed", 13, True),
            (4, "interrupted", 14, False),
            (5, None, 15, True),
        )
    ]
    http = FakeHttpClient([
        _envelope({"turns": turns, "has_more": False}),
        _envelope({"recorded": True, "changes": [_change()]}),
    ])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        changes = await client.get_file_changes("session-1")
    assert changes.turn == RecordedTurn(13)
    assert changes.changes[0].additions == 1
    assert http.requests[1][2]["params"] == {"turn_number": 13}


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
        {"turn_number": 7, "path": "fixture.txt", "phase": phase}
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
        (True, _change(path="another.txt"), None, "not_recorded"),
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


@pytest.mark.parametrize("environment_id,cwd", [("local", None), ("remote", "/target")])
async def test_hidden_workspace_resolves_native_alias_membership_across_pages(
    environment_id: str, cwd: str | None,
):
    binding = {"workspace_id": "old-id", "environment_id": environment_id}
    if cwd is not None:
        binding["cwd"] = cwd
    alias = {"id": "visible-id", "root": "/primary", "additional_dirs": ["/extra"]}
    http = FakeHttpClient(
        [
            _envelope({"metadata": {"cwd": "/original"}, "busy": True}),
            _envelope(binding),
            _envelope({"items": [
                {"id": "unrelated", "root": "/original", "additional_dirs": []},
                alias,
            ]}),
            _envelope({"items": [{"id": "other", "metadata": {"cwd": "/original"}}]}),
            _envelope({"items": [{"id": f"older-{index}"} for index in range(50)]}),
            _envelope({"items": [{"id": "session-1", "workspace_id": "old-id"}]}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        context = await client.get_file_context("session-1")
    assert context == FileContext(
        WorkspaceInfo("visible-id", "/primary", ("/extra",)),
        cwd or "/original", environment_id,
    )
    membership = [request[2]["params"] for request in http.requests[3:]]
    assert [query["workspace_id"] for query in membership] == [
        "unrelated", "visible-id", "visible-id",
    ]
    assert membership[-1]["before_id"] == "older-49"
    assert all(query["include_archive"] and "busy" not in query for query in membership)
    assert all(method == "GET" for method, _, _ in http.requests)


async def test_missing_workspace_does_not_guess_from_matching_session_cwd():
    http = FakeHttpClient(
        [
            _envelope({"metadata": {"cwd": "/original"}}),
            _envelope({"workspace_id": "missing", "environment_id": "local"}),
            _envelope({"items": [{
                "id": "unrelated", "root": "/original", "additional_dirs": [],
            }]}),
            _envelope({"items": [{"id": "other", "metadata": {"cwd": "/original"}}]}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        with pytest.raises(KimiServerOperationError):
            await client.get_file_context("session-1")
    assert all(method == "GET" for method, _, _ in http.requests)


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
    http = FakeHttpClient([_plan_response("TARGET PLAN", str(host))])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        document = await client.get_plan_document(request)
    assert document.text == "TARGET PLAN" and document.name == "plan.md"
    assert http.requests[0][1].endswith("/sessions/session-1/agents/child/turns")
    assert http.requests[0][2]["params"] == {"page_size": 200}
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
            if path.endswith("/turns"):
                data = {
                    "turns": [
                        _turn(
                            f"turn-{self.latest}",
                            ordinal=self.latest,
                            status="ended",
                            outcome="completed",
                            turn_number=self.latest,
                            steps=[{}],
                        )
                    ],
                    "has_more": False,
                }
            elif path.endswith("/file-history/changes"):
                data = {
                    "recorded": True,
                    "changes": [_change("deleted", path="deleted.txt")]
                    if kwargs["params"]["turn_number"] == 7
                    else [],
                }
            elif path.endswith("/file-history/content"):
                self.content_turns.append(kwargs["params"]["turn_number"])
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
        result = await client.get_file_diff("session-fixture", "current.txt")
        assert result.unavailable == "not_recorded"
    finally:
        await router.close()


@pytest.mark.parametrize(
    "before,after",
    [("value", "value\n"), ("value\n", "value"), ("value\r\n", "value\n")],
)
async def test_recorded_diff_preserves_line_terminator_changes(before, after):
    http = FakeHttpClient(
        [
            _envelope({"recorded": True, "changes": [_change()]}),
            *_context_responses(),
            _envelope({"content": {"version": 1, "content": before}}),
            _envelope({"content": {"version": 2, "content": after}}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff(
            "session-1", "fixture.txt", turn=RecordedTurn(1)
        )
    assert "-value" in diff.text and "+value" in diff.text
    if not before.endswith("\n") or not after.endswith("\n"):
        assert "\\ No newline at end of file" in diff.text
    if before.endswith("\r\n"):
        assert "-value\r\n" in diff.text


@pytest.mark.parametrize("case", ["repetitive", "lines", "characters"])
async def test_large_repetitive_diff_is_rejected_before_expensive_matching(case):
    from kimi_bridge.kimi_server.file_history import _text_diff

    repetitive = "\n".join(f"label {number % 100}" for number in range(100000))
    if case == "lines":
        repetitive = "x\n" * 2001
    elif case == "characters":
        repetitive = "x" * 131073
    diff = _text_diff(repetitive, "new\n" + repetitive, "fixture.txt")
    assert diff.unavailable == "oversize" and diff.text is None


@pytest.mark.parametrize("prefix", ["~", "～"])
async def test_local_recorded_diff_expands_home_paths(tmp_path, monkeypatch, prefix):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    from test_workspace_files import FileServer

    # The native fake uses real local paths for containment; history stays recorded data.
    server = FileServer(tmp_path)
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "recorded": True,
                    "changes": [_change(path=str(tmp_path / "fixture.txt"))],
                }
            ),
            _envelope({"metadata": {"cwd": str(tmp_path)}}),
            _envelope({"workspace_id": "workspace-fixture", "environment_id": "local"}),
            _envelope({"items": [server.workspace]}),
            _envelope({"content": {"content": "before\n"}}),
            _envelope({"content": {"content": "after\n"}}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff(
            "session-1", prefix + "/fixture.txt", turn=RecordedTurn(1)
        )
    assert diff.unavailable is None and "+after" in diff.text


async def test_decoded_plan_approval_uses_native_projection_in_chat(tmp_path):
    from kimi_bridge.kimi_server.wire import _interaction_update_from_wire
    from test_kimi_server import _interaction_frame

    frame = {
        **_interaction_frame("approval"),
        "session_id": "session-control",
        "agent_id": "child",
        "tool_call_id": "plan-tool",
    }
    frame["request"]["tool_name"] = "ExitPlanMode"
    request = _interaction_update_from_wire(frame).request
    http = FakeHttpClient([
        _plan_response("Projected plan fixture", "/target/plan.md")
    ])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as native:

        class Client(FakeKimiClient):
            async def get_plan_document(self, value):
                return await native.get_plan_document(value)

        client = Client()
        client.sessions = [_control_session(busy=True)]
        client.approvals["session-control"] = [request]
        router, _ = _router(tmp_path, client)
        adapter = FakeAdapter()
        try:
            from test_router import _wait_for

            await router.handle_inbound(adapter, _message("/status"))
            await _wait_for(lambda: bool(adapter.interactions))
            assert adapter.interactions[0][2].plan_preview == "Projected plan fixture"
            assert http.requests[0][1].endswith(
                "/sessions/session-control/agents/child/turns"
            )
            assert http.requests[0][2]["params"] == {"page_size": 200}
        finally:
            await router.close()


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85", "\v", "\f"])
def test_recorded_diff_keeps_unicode_separators_inside_physical_lines(separator):
    from kimi_bridge.kimi_server.file_history import _text_diff

    diff = _text_diff(
        "before" + separator + "tail\n", "after" + separator + "tail\n", "fixture.txt"
    )
    assert "-before" + separator + "tail\n" in diff.text
    assert "+after" + separator + "tail\n" in diff.text
    assert "\\ No newline at end of file" not in diff.text


async def test_recorded_deleted_fullwidth_tilde_path_is_literal(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    # The recorded literal no longer exists. Another recorded file is at the expanded path.
    literal = "～/fixture.txt"
    expanded = str(tmp_path / "home" / "fixture.txt")
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "recorded": True,
                    "changes": [
                        _change("deleted", path=literal),
                        _change(path=expanded),
                    ],
                }
            ),
            _envelope({"metadata": {"cwd": str(tmp_path)}}),
            _envelope({"workspace_id": "workspace", "environment_id": "local"}),
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
            _envelope({"content": {"content": "Recorded deleted fixture\n"}}),
            _envelope({"content": {"version": 2}}),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token", http_client=http
    ) as client:
        diff = await client.get_file_diff("session-1", literal, turn=RecordedTurn(1))
    assert "-Recorded deleted fixture" in diff.text
    assert all(
        request[2]["params"]["path"] == literal
        for request in http.requests
        if request[1].endswith("/file-history/content")
    )
