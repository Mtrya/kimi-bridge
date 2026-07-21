from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from kimi_bridge.platforms.base import InboundMessage
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import BridgeState, ConversationBinding, StateStore


class FakeKimiClient:
    def __init__(self) -> None:
        self.created: list[tuple[str, str | None, dict[str, Any]]] = []
        self.prompts: list[tuple[str, str, dict[str, Any]]] = []
        self.aborted: list[str] = []
        self.abort_result = True
        self.sessions: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []
        self.resumed: list[str] = []
        self.subscriptions: list[str] = []
        self.stream_actions: list[tuple[str, str]] = []
        self.snapshots: dict[str, dict[str, Any]] = {}
        self._events: dict[str, asyncio.Queue[dict[str, Any]]] = {}
        self._ready: dict[str, asyncio.Event] = {}

    async def create_session(
        self,
        workspace: str,
        *,
        title: str | None = None,
        **profile: Any,
    ) -> str:
        session_id = f"session-{len(self.created) + 1}"
        self.created.append((workspace, title, profile))
        self.sessions.insert(
            0,
            {
                "id": session_id,
                "title": title or "",
                "busy": False,
                "metadata": {"cwd": workspace},
            },
        )
        return session_id

    async def submit_prompt(
        self, session_id: str, text: str, **profile: Any
    ) -> dict[str, Any]:
        self.prompts.append((session_id, text, profile))
        return {"prompt_id": f"prompt-{len(self.prompts)}"}

    async def list_sessions(self, **params: Any) -> list[dict[str, Any]]:
        self.list_calls.append(params)
        sessions = [
            session
            for session in self.sessions
            if bool(session.get("busy")) is params["busy"]
        ]
        return sessions[: params.get("page_size")]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return next(session for session in self.sessions if session["id"] == session_id)

    async def resume_session(self, session_id: str) -> None:
        self.resumed.append(session_id)
        self.stream_actions.append(("resume", session_id))

    async def abort_prompt(self, session_id: str) -> bool:
        self.aborted.append(session_id)
        return self.abort_result

    async def get_snapshot(self, session_id: str) -> dict[str, Any]:
        return self.snapshots.get(
            session_id,
            {"in_flight_turn": None, "messages": {"items": []}},
        )

    async def wait_until_subscribed(
        self, session_id: str, *, timeout: float = 1
    ) -> None:
        ready = self._ready.setdefault(session_id, asyncio.Event())
        await asyncio.wait_for(ready.wait(), timeout)

    async def subscribe_events(self, session_id: str):
        self.subscriptions.append(session_id)
        self.stream_actions.append(("subscribe", session_id))
        queue = self._events.setdefault(session_id, asyncio.Queue())
        self._ready.setdefault(session_id, asyncio.Event()).set()
        while True:
            yield await queue.get()

    def emit(self, session_id: str, event: dict[str, Any]) -> None:
        self._events.setdefault(session_id, asyncio.Queue()).put_nowait(event)


class FakeAdapter:
    name = "feishu"

    def __init__(self, *, message_limit: int = 1000) -> None:
        self.message_limit = message_limit
        self.sent: list[tuple[str, str, str]] = []
        self.edits: list[tuple[str, str]] = []

    async def start(self, _handler: Any) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send_text(self, user_id: str, text: str) -> str:
        message_id = f"message-{len(self.sent) + 1}"
        self.sent.append((message_id, user_id, text))
        return message_id

    async def edit_text(self, message_id: str, text: str) -> None:
        self.edits.append((message_id, text))


def _message(text: str) -> InboundMessage:
    return InboundMessage(
        platform="feishu",
        bot_id="cli_bot",
        user_id="ou_user",
        user_name=None,
        text=text,
        timestamp=1.0,
    )


async def test_first_message_creates_auto_session_and_persists_binding(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    workspace = tmp_path / "workspace"
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=workspace,
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("  hello from Feishu  "))
    finally:
        await router.close()

    assert client.created == [
        (
            str(workspace.resolve()),
            "hello from Feishu",
            {"permission_mode": "auto"},
        )
    ]
    assert client.prompts == [
        (
            "session-1",
            "hello from Feishu",
            {"model": "kimi-code/k3", "permission_mode": "auto"},
        )
    ]
    binding = store.load().bindings["feishu:cli_bot:ou_user"]
    assert binding.session_id == "session-1"
    assert binding.workspace == str(workspace.resolve())
    assert binding.permission_mode == "auto"


async def test_persisted_binding_is_resumed_before_websocket_subscription(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            bindings={
                "feishu:cli_bot:ou_user": ConversationBinding(
                    session_id="session-restored",
                    workspace=str(tmp_path),
                )
            }
        )
    )
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("after restart"))
    finally:
        await router.close()

    assert client.stream_actions == [
        ("resume", "session-restored"),
        ("subscribe", "session-restored"),
    ]
    assert client.prompts == [
        (
            "session-restored",
            "after restart",
            {"model": "kimi-code/k3", "permission_mode": "auto"},
        )
    ]


async def test_bridge_commands_are_intercepted_and_rebind_sessions(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    client.sessions = [
        {
            "id": "session-a",
            "title": "Alpha",
            "busy": False,
            "metadata": {"cwd": "/tmp/alpha"},
        },
        {
            "id": "session-b",
            "title": "Beta",
            "busy": True,
            "metadata": {"cwd": "/tmp/beta"},
        },
    ]
    adapter = FakeAdapter()
    store = StateStore(tmp_path / "state.json")
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("/help"))
        await router.handle_inbound(adapter, _message("/sessions"))
        await router.handle_inbound(adapter, _message("/switch 2"))
        await router.handle_inbound(adapter, _message("/stop"))
        await router.handle_inbound(adapter, _message("/mode manual"))
    finally:
        await router.close()

    texts = [text for _message_id, _user_id, text in adapter.sent]
    assert any("/new [cwd]" in text for text in texts)
    assert any("Alpha [idle]" in text and "Beta [busy]" in text for text in texts)
    assert any("Switched to session-b" in text for text in texts)
    assert any(text == "Stopped." for text in texts)
    assert any("Unknown command: /mode" in text for text in texts)
    assert client.aborted == ["session-b"]
    assert client.prompts == []
    assert client.list_calls == [
        {"busy": False, "page_size": 10},
        {"busy": True, "page_size": 10},
    ]
    assert store.load().bindings["feishu:cli_bot:ou_user"].session_id == "session-b"


async def test_new_command_uses_requested_workspace_without_forwarding(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    project = tmp_path / "project"
    project.mkdir()
    store = StateStore(tmp_path / "state.json")
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=store,
        default_workspace=tmp_path / "scratch",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message(f"/new {project}"))
    finally:
        await router.close()

    assert client.created[0][0] == str(project.resolve())
    assert client.created[0][2] == {"permission_mode": "auto"}
    assert client.prompts == []
    assert store.load().bindings["feishu:cli_bot:ou_user"].workspace == str(
        project.resolve()
    )


async def test_delta_throttle_final_edit_and_router_chunking(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter(message_limit=4)
    release_flush = asyncio.Event()
    delays: list[float] = []
    now = [100.0]

    async def controlled_sleep(delay: float) -> None:
        delays.append(delay)
        await release_flush.wait()

    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
        edit_throttle_seconds=1.5,
        sleep=controlled_sleep,
        clock=lambda: now[0],
    )
    try:
        await router.handle_inbound(adapter, _message("hello"))
        client.emit("session-1", _event("turn.started"))
        client.emit("session-1", _event("assistant.delta", delta="abc", offset=0))
        await _wait_for(lambda: len(adapter.sent) == 1)
        assert adapter.sent[0][2] == "abc"

        client.emit("session-1", _event("assistant.delta", delta="def", offset=3))
        await _wait_for(lambda: bool(delays))
        assert adapter.edits == []
        assert len(adapter.sent) == 1

        release_flush.set()
        await _wait_for(lambda: len(adapter.sent) == 2 and bool(adapter.edits))
        assert adapter.edits == [("message-1", "abcd")]
        assert adapter.sent[1][2] == "ef"

        client.snapshots["session-1"] = {
            "in_flight_turn": None,
            "messages": {
                "items": [
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "abcdefgh"}],
                    }
                ]
            },
        }
        client.emit("session-1", _event("turn.ended"))
        await _wait_for(
            lambda: ("message-2", "efgh") in adapter.edits
        )
    finally:
        await router.close()

    assert delays == [1.5]
    assert adapter.sent == [
        ("message-1", "ou_user", "abc"),
        ("message-2", "ou_user", "ef"),
    ]
    assert adapter.edits[-1] == ("message-2", "efgh")


async def test_resync_snapshot_rebuilds_in_flight_stream(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter(message_limit=4)
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("hello"))
        client.emit(
            "session-1",
            {
                "type": "resync_required",
                "payload": {"type": "resync_required"},
                "snapshot": {
                    "in_flight_turn": {"assistant_text": "abcdefghi"},
                    "messages": {"items": []},
                },
            },
        )
        await _wait_for(lambda: len(adapter.sent) == 3)
    finally:
        await router.close()

    assert [text for _id, _user, text in adapter.sent] == [
        "abcd",
        "efgh",
        "i",
    ]


async def test_messages_during_a_turn_queue_on_the_same_session(
    tmp_path: Path,
) -> None:
    client = FakeKimiClient()
    adapter = FakeAdapter()
    router = ChatRouter(
        client,  # type: ignore[arg-type]
        state_store=StateStore(tmp_path / "state.json"),
        default_workspace=tmp_path / "workspace",
        model="kimi-code/k3",
    )
    try:
        await router.handle_inbound(adapter, _message("first"))
        await router.handle_inbound(adapter, _message("second"))
    finally:
        await router.close()

    assert len(client.created) == 1
    assert [prompt[:2] for prompt in client.prompts] == [
        ("session-1", "first"),
        ("session-1", "second"),
    ]
    assert client.subscriptions == ["session-1"]


def _event(
    event_type: str, *, delta: str | None = None, offset: int | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {"type": event_type}
    if delta is not None:
        payload["delta"] = delta
    event: dict[str, Any] = {"type": event_type, "payload": payload}
    if offset is not None:
        event["offset"] = offset
    return event


async def _wait_for(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")
