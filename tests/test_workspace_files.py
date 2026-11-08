from __future__ import annotations

import mimetypes
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from kimi_bridge.kimi_server import KimiServerClient, KimiServerAPIError
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import BridgeState, ConversationBinding, StateStore
from test_router import FakeAdapter, FakeKimiClient, _message


class FileServer:
    """A native filesystem transport fake backed by disposable real files."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.primary = cwd
        self.additional = []
        self.requests = []

    @property
    def workspace(self):
        return {
            "id": "workspace-fixture",
            "root": str(self.primary),
            "additional_dirs": [str(path) for path in self.additional],
        }

    async def request(self, method, url, **kwargs):
        self.requests.append((method, url, kwargs))
        path = unquote(httpx.URL(url).path)
        request = httpx.Request(method, url)

        def response(data=None, code=0, message="fixture"):
            return httpx.Response(
                200, json={"code": code, "data": data, "msg": message}, request=request
            )

        if path.endswith("/environment"):
            return response(
                {"workspace_id": "workspace-fixture", "environment_id": "local"}
            )
        if path == "/api/v1/workspaces":
            return response({"items": [self.workspace]})
        if "/workspaces/" in path:
            body = kwargs["json"]
            if method == "PATCH":
                entries = [Path(value).resolve() for value in body["additional_dirs"]]
                if self.primary in entries or len(set(entries)) != len(entries):
                    return response(code=40006, message="duplicate folder")
                if any(not value.is_dir() for value in entries):
                    return response(code=40409, message="folder missing")
                self.additional = entries
            elif path.endswith("/primary"):
                selected = Path(body["path"])
                if not selected.is_absolute():
                    selected = self.primary / selected
                if selected not in self.additional:
                    return response(code=40001, message="not a workspace folder")
                self.primary, self.additional = (
                    selected,
                    [
                        self.primary,
                        *(value for value in self.additional if value != selected),
                    ],
                )
            return response(self.workspace)
        if "/fs:" not in path and "/fs/" not in path:
            return response(
                {
                    "id": path.rsplit("/", 1)[-1],
                    "workspace_id": "workspace-fixture",
                    "metadata": {"cwd": str(self.cwd)},
                }
            )
        body = kwargs.get("json", {})
        argument = body.get("path", ".")
        if "/fs/" in path:
            argument = path.partition("/fs/")[2][: -len(":download")]
        requested = Path(argument).expanduser()
        resolved = (
            requested if requested.is_absolute() else self.cwd / requested
        ).resolve()
        if not any(
            resolved.is_relative_to(root.resolve())
            for root in [self.cwd, self.primary, *self.additional]
        ):
            return response(code=41304, message="path escapes workspace")
        if not resolved.exists():
            return response(code=40409, message="file missing")
        if path.endswith("fs:list") or path.endswith("fs:search"):
            candidates = (
                sorted(resolved.iterdir())
                if path.endswith("fs:list")
                else sorted(self.cwd.rglob("*"))
            )
            query = body.get("query")
            if query:
                candidates = [value for value in candidates if query in value.name]
            items = [
                {
                    "path": str(value.relative_to(self.cwd))
                    if value.is_relative_to(self.cwd)
                    else value.name,
                    "name": value.name,
                    "kind": "directory" if value.is_dir() else "file",
                    "size": value.stat().st_size,
                }
                for value in candidates
            ]
            limit = body["limit"]
            return response({"items": items[:limit], "truncated": len(items) > limit})
        if resolved.is_dir():
            return response(code=41301, message="path is a directory")
        data = resolved.read_bytes()
        if path.endswith("fs:read"):
            binary = b"\x00" in data
            return response(
                {
                    "path": argument,
                    "content": "" if binary else data[: body["length"]].decode("utf-8"),
                    "encoding": "base64" if binary else "utf-8",
                    "size": len(data),
                    "is_binary": binary,
                    "truncated": len(data) > body["length"],
                }
            )
        return httpx.Response(
            200,
            content=data,
            headers={
                "content-disposition": "attachment",
                "content-type": mimetypes.guess_type(resolved.name)[0]
                or "application/octet-stream",
            },
            request=request,
        )


class FileClient(FakeKimiClient):
    def __init__(self, cwd: Path):
        super().__init__()
        self.server = FileServer(cwd)
        self.native = KimiServerClient(
            "http://127.0.0.1:43123", "token", http_client=self.server
        )

    async def get_file_context(self, session_id):
        return await self.native.get_file_context(session_id)

    async def change_workspace(self, session_id, action, path):
        return await self.native.change_workspace(session_id, action, path)

    async def list_workspace_files(self, session_id, directory=".", *, query=None):
        return await self.native.list_workspace_files(
            session_id, directory, query=query
        )

    async def preview_workspace_file(self, session_id, path):
        return await self.native.preview_workspace_file(session_id, path)

    async def download_workspace_file(self, session_id, path):
        return await self.native.download_workspace_file(session_id, path)


def _setup(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    client = FileClient(workspace)
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            bindings={
                "feishu:cli_bot:ou_user": ConversationBinding(
                    "session-fixture", str(workspace)
                )
            }
        )
    )
    return (
        workspace,
        client,
        store,
        FakeAdapter(),
        ChatRouter(
            client, state_store=store, default_workspace=workspace, model="kimi-code/k3"
        ),
    )


async def test_file_choices_replace_and_numeric_space_paths_use_native_access(
    tmp_path: Path,
):
    workspace, client, _, adapter, router = _setup(tmp_path)
    (workspace / "123").write_text("numeric fixture")
    (workspace / "notes with spaces.txt").write_text("space fixture")
    try:
        await router.handle_inbound(adapter, _message("/files"))
        await router.handle_inbound(adapter, _message("/files show 1"))
        assert "numeric fixture" in adapter.sent[-1][2]
        await router.handle_inbound(adapter, _message("/files search spaces"))
        await router.handle_inbound(adapter, _message("/files send 1"))
        assert adapter.files[-1][2].data == b"space fixture"
        await router.handle_inbound(adapter, _message("/files show ./123"))
        assert "numeric fixture" in adapter.sent[-1][2]
        await router.handle_inbound(
            adapter, _message("/files send notes with spaces.txt")
        )
        assert adapter.files[-1][2].name == "notes with spaces.txt"
        sent = len(adapter.files)
        await router.handle_inbound(adapter, _message("/send notes with spaces.txt"))
        assert len(adapter.files) == sent
        read_requests = [
            request
            for request in client.server.requests
            if request[1].endswith("fs:read")
        ]
        assert all(
            request[2]["json"]["allow_external_symlinks"] is False
            for request in read_requests
        )
    finally:
        await router.close()


async def test_workspace_membership_is_shared_and_invalidates_file_numbers(
    tmp_path: Path,
):
    workspace, client, store, adapter, router = _setup(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "extra.txt").write_text("extra fixture")
    state = store.load()
    state.bindings["feishu:cli_bot:ou_other"] = ConversationBinding(
        "session-2", str(workspace), render_thinking=True
    )
    store.save(state)
    router._state = store.load()
    (workspace / "main.txt").write_text("main fixture")
    try:
        await router.handle_inbound(adapter, _message("/files"))
        await router.handle_inbound(adapter, _message(f"/workspace add {extra}"))
        await router.handle_inbound(adapter, _message("/files send 1"))
        assert adapter.files == []
        await router.handle_inbound(adapter, _message(f"/files {extra}"))
        await router.handle_inbound(adapter, _message("/files send 1"))
        assert adapter.files[-1][2].data == b"extra fixture"
        await router.handle_inbound(adapter, _message(f"/workspace primary {extra}"))
        assert client.server.primary == extra
        assert client.server.additional == [workspace]
        assert store.load().bindings["feishu:cli_bot:ou_other"].workspace == str(
            workspace
        )
        assert store.load().bindings["feishu:cli_bot:ou_other"].render_thinking
        await router.handle_inbound(adapter, _message(f"/workspace remove {workspace}"))
        assert client.server.additional == []
        # Native sessions retain their original cwd after a shared primary swap.
        await router.handle_inbound(adapter, _message("/files send main.txt"))
        assert adapter.files[-1][2].data == b"main fixture"
    finally:
        await router.close()


async def test_native_preview_bounds_binary_and_download_error_envelope(tmp_path: Path):
    workspace, client, _, adapter, router = _setup(tmp_path)
    (workspace / "large.txt").write_text("x" * 12000)
    (workspace / "binary.bin").write_bytes(b"\x00binary")
    try:
        preview = await client.preview_workspace_file("session-1", "large.txt")
        assert preview.text == "x" * 8192 and preview.truncated
        preview = await client.preview_workspace_file("session-1", "binary.bin")
        assert preview.binary and preview.text is None
        with pytest.raises(KimiServerAPIError) as error:
            await client.download_workspace_file("session-1", "missing.txt")
        assert error.value.code == 40409
        assert not adapter.files
    finally:
        await router.close()


async def test_file_numbers_are_scoped_to_chat_and_current_native_context(
    tmp_path: Path,
):
    workspace, client, _, adapter, router = _setup(tmp_path)
    (workspace / "fixture.txt").write_text("fixture")
    other = tmp_path / "extra"
    other.mkdir()
    try:
        await router.handle_inbound(adapter, _message("/files"))
        await router.handle_inbound(
            adapter, _message("/files send 1", conversation_id="other-chat")
        )
        assert adapter.files == []
        # Native membership can change through another client.
        client.server.additional = [other]
        await router.handle_inbound(adapter, _message("/files send 1"))
        assert adapter.files == []
        await router.handle_inbound(adapter, _message("/files send fixture.txt"))
        assert adapter.files[-1][2].data == b"fixture"
        await router.handle_inbound(adapter, _message("/files"))
        await router.handle_inbound(adapter, _message("/new"))
        await router.handle_inbound(adapter, _message("/files send 1"))
        assert len(adapter.files) == 1
    finally:
        await router.close()


async def test_failed_file_list_display_does_not_install_new_numbers(tmp_path: Path):
    workspace, client, _, _, router = _setup(tmp_path)
    for number in range(5):
        (workspace / f"long-fixture-name-{number}.txt").write_text("fixture")

    class PartialAdapter(FakeAdapter):
        async def send_final_text(self, conversation, text):
            if self.final_texts:
                raise OSError("fixture send interrupted")
            return await super().send_final_text(conversation, text)

    adapter = PartialAdapter(message_limit=40)
    try:
        with pytest.raises(OSError):
            await router.handle_inbound(adapter, _message("/files"))
        assert router._file_choices == {}
    finally:
        await router.close()


async def test_workspace_rejections_leave_native_folder_set_intact(tmp_path: Path):
    workspace, client, _, _, router = _setup(tmp_path)
    extra = tmp_path / "extra"
    extra.mkdir()
    try:
        await client.change_workspace("session-1", "add", str(extra))
        with pytest.raises(KimiServerAPIError):
            await client.change_workspace("session-1", "add", str(extra))
        with pytest.raises(KimiServerAPIError):
            await client.change_workspace("session-1", "add", str(tmp_path / "missing"))
        assert client.server.additional == [extra]
        assert client.server.primary == workspace and extra.exists()
    finally:
        await router.close()
