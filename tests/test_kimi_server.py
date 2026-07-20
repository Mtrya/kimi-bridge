from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from typing import Any

import pytest

from kimi_bridge.kimi_server import (
    KimiServerAPIError,
    KimiServerClient,
    KimiServerSupervisor,
    ServerConnection,
    parse_server_startup_line,
)


def _envelope(data: Any, *, code: int = 0, msg: str = "success") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data, "request_id": "req-1"}


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: Iterable[dict[str, Any]]) -> None:
        self._responses = iter(responses)
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        return FakeResponse(next(self._responses))


class FakeWebSocket:
    def __init__(
        self,
        *,
        subscribe_payload: dict[str, Any],
        events: Iterable[dict[str, Any]] = (),
        events_before_ack: bool = False,
    ) -> None:
        self._subscribe_payload = subscribe_payload
        self._events = list(events)
        self._events_before_ack = events_before_ack
        self._incoming: asyncio.Queue[str] = asyncio.Queue()
        self._incoming.put_nowait(
            json.dumps(
                {
                    "type": "server_hello",
                    "timestamp": "now",
                    "payload": {
                        "ws_connection_id": "conn-1",
                        "protocol_version": 2,
                        "max_event_buffer_size": 1000,
                        "capabilities": {
                            "event_batching": False,
                            "compression": False,
                        },
                    },
                }
            )
        )
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def __aenter__(self) -> FakeWebSocket:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pass

    async def send(self, raw_frame: str) -> None:
        frame = json.loads(raw_frame)
        self.sent.append(frame)
        if frame["type"] == "client_hello":
            self._incoming.put_nowait(
                json.dumps(
                    {
                        "type": "ack",
                        "id": frame["id"],
                        "code": 0,
                        "msg": "success",
                        "payload": {
                            "accepted_subscriptions": [],
                            "resync_required": [],
                            "cursors": {},
                        },
                    }
                )
            )
        elif frame["type"] == "subscribe":
            if self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))
            self._incoming.put_nowait(
                json.dumps(
                    {
                        "type": "ack",
                        "id": frame["id"],
                        "code": 0,
                        "msg": "success",
                        "payload": self._subscribe_payload,
                    }
                )
            )
            if not self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))

    async def recv(self) -> str:
        return await self._incoming.get()

    async def close(self) -> None:
        self.closed = True


class FakeWebSocketConnect:
    def __init__(self, sockets: Iterable[FakeWebSocket]) -> None:
        self._sockets = iter(sockets)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeWebSocket:
        self.calls.append((url, kwargs))
        return next(self._sockets)


class FakeProcess:
    def __init__(self, startup_line: str) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data((startup_line + "\n").encode())
        self.returncode: int | None = None
        self._done = asyncio.Event()
        self.terminated = False
        self.killed = False

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode

    def crash(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout.feed_eof()
        self._done.set()

    def terminate(self) -> None:
        self.terminated = True
        self.crash(0)

    def kill(self) -> None:
        self.killed = True
        self.crash(-9)


class FakeProcessFactory:
    def __init__(self, processes: Iterable[FakeProcess]) -> None:
        self._processes = iter(processes)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> FakeProcess:
        self.calls.append((args, kwargs))
        return next(self._processes)


def _session_event(
    seq: int, epoch: str, event_type: str, *, volatile: bool = False
) -> dict[str, Any]:
    event = {
        "type": event_type,
        "seq": seq,
        "epoch": epoch,
        "session_id": "session-1",
        "timestamp": "now",
        "payload": {"type": event_type, "delta": event_type},
    }
    if volatile:
        event.update({"volatile": True, "offset": 0})
    return event


def test_parses_contract_and_ansi_startup_lines_without_exposing_token() -> None:
    token = "secret_token-123"
    assert parse_server_startup_line(
        f"Kimi server: http://127.0.0.1:43123/#token={token}"
    ) == (43123, token)
    assert parse_server_startup_line(
        f"\x1b[1mLocal:\x1b[0m http://127.0.0.1:43123/#token={token}\x1b[0m"
    ) == (43123, token)
    assert token not in repr(
        ServerConnection(
            base_url="http://127.0.0.1:43123",
            port=43123,
            generation=1,
            token=token,
        )
    )


async def test_rest_methods_use_snapshotted_paths_and_shapes(tmp_path: Any) -> None:
    http = FakeHttpClient(
        [
            _envelope({"server_version": "0.27.0"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"id": "session-1"}),
            _envelope(
                {
                    "id": "session-1",
                    "metadata": {"cwd": str(tmp_path.resolve())},
                }
            ),
            _envelope({"prompt_id": "prompt-1", "status": "running"}),
            _envelope(
                {
                    "active": {"prompt_id": "prompt-1"},
                    "queued": [],
                }
            ),
            _envelope({"aborted": True}),
            _envelope({"as_of_seq": 4, "epoch": "epoch-1"}),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    assert await client.check_server_version() == "0.27.0"
    assert await client.get_config() == {"default_model": "kimi-code/k3"}
    assert await client.get_default_model() == "kimi-code/k3"
    assert await client.create_session(
        str(tmp_path), title="Test session", permission_mode="auto"
    ) == "session-1"
    assert (await client.get_session("session-1"))["id"] == "session-1"
    await client.submit_prompt(
        "session-1",
        "hello",
        model="kimi-code/k3",
        permission_mode="auto",
    )
    assert await client.abort_prompt("session-1") is True
    assert await client.get_snapshot("session-1") == {
        "as_of_seq": 4,
        "epoch": "epoch-1",
    }

    assert [request[0:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/api/v1/meta"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-1:abort",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/snapshot"),
    ]
    assert http.requests[3][2]["json"] == {
        "metadata": {"cwd": str(tmp_path.resolve())},
        "title": "Test session",
        "agent_config": {"permission_mode": "auto"},
    }
    assert http.requests[5][2]["json"] == {
        "content": [{"type": "text", "text": "hello"}],
        "model": "kimi-code/k3",
        "permission_mode": "auto",
    }
    assert all(
        request[2]["headers"] == {"Authorization": "Bearer token-1"}
        for request in http.requests
    )


async def test_rest_envelope_error_is_raised() -> None:
    http = FakeHttpClient([_envelope(None, code=40401, msg="not found")])
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    with pytest.raises(KimiServerAPIError) as caught:
        await client.get_snapshot("missing")

    assert caught.value.code == 40401


async def test_epoch_change_resyncs_from_snapshot_and_reuses_cursor() -> None:
    first = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {"session-1": {"seq": 5, "epoch": "epoch-old"}},
        },
        events=[
            _session_event(
                5, "epoch-old", "assistant.delta", volatile=True
            ),
            _session_event(6, "epoch-old", "assistant.delta"),
            _session_event(7, "epoch-new", "turn.started"),
        ],
    )
    second = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[
            _session_event(8, "epoch-new", "assistant.delta"),
            _session_event(9, "epoch-new", "turn.ended"),
        ],
    )
    ws_connect = FakeWebSocketConnect([first, second])
    http = FakeHttpClient(
        [_envelope({"as_of_seq": 8, "epoch": "epoch-new"})]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )
    events = client.subscribe_events("session-1")

    volatile = await asyncio.wait_for(anext(events), 1)
    assert volatile["seq"] == 5
    assert volatile["volatile"] is True
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 6
    resync = await asyncio.wait_for(anext(events), 1)
    assert resync["type"] == "resync_required"
    assert resync["payload"]["reason"] == "epoch_changed"
    assert resync["snapshot"] == {
        "as_of_seq": 8,
        "epoch": "epoch-new",
    }
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 9
    await events.aclose()

    assert http.requests[0][0:2] == (
        "GET",
        "http://127.0.0.1:43123/api/v1/sessions/session-1/snapshot",
    )
    subscribe = next(
        frame for frame in second.sent if frame["type"] == "subscribe"
    )
    assert subscribe["payload"]["cursors"] == {
        "session-1": {"seq": 8, "epoch": "epoch-new"}
    }
    assert ws_connect.calls[0] == (
        "ws://127.0.0.1:43123/api/v1/ws",
        {
            "additional_headers": {"Authorization": "Bearer token-1"},
            "ping_interval": None,
        },
    )


async def test_stored_session_not_loaded_after_restart_retries_from_snapshot() -> None:
    first = FakeWebSocket(
        subscribe_payload={
            "accepted": [],
            "not_found": ["session-1"],
            "resync_required": [],
            "cursors": {},
        }
    )
    second = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[_session_event(11, "epoch-new", "turn.ended")],
        events_before_ack=True,
    )
    ws_connect = FakeWebSocketConnect([first, second])
    http = FakeHttpClient(
        [_envelope({"as_of_seq": 10, "epoch": "epoch-new"})]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
        sleep=fake_sleep,
    )
    events = client.subscribe_events("session-1")

    resync = await asyncio.wait_for(anext(events), 1)
    assert resync["payload"]["reason"] == "session_not_loaded"
    assert resync["snapshot"]["as_of_seq"] == 10
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 11
    await events.aclose()

    assert delays == [0.25]
    subscribe = next(
        frame for frame in second.sent if frame["type"] == "subscribe"
    )
    assert subscribe["payload"]["cursors"] == {
        "session-1": {"seq": 10, "epoch": "epoch-new"}
    }


async def test_supervisor_restarts_with_exponential_backoff() -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    first = FakeProcess(startup)
    second = FakeProcess(startup)
    third = FakeProcess(startup)
    factory = FakeProcessFactory([first, second, third])
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        await asyncio.sleep(0)

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        initial_backoff=0.1,
        max_backoff=0.2,
        process_factory=factory,
        sleep=fake_sleep,
    )
    try:
        assert (await supervisor.start()).generation == 1
        first.crash(7)
        assert (
            await supervisor.wait_until_ready(after_generation=1)
        ).generation == 2
        second.crash(8)
        assert (
            await supervisor.wait_until_ready(after_generation=2)
        ).generation == 3
    finally:
        await supervisor.stop()

    assert delays == [0.1, 0.2]
    assert third.terminated is True
    assert factory.calls[0][0] == (
        "kimi",
        "server",
        "run",
        "--foreground",
        "--port",
        "43123",
        "--keep-alive",
    )
