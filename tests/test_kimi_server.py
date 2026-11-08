from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from typing import Any

import httpx
import pytest

from kimi_bridge.compatibility import (
    KIMI_CODE_INSTALL_URL,
    KimiExecutableIdentity,
    KimiProduct,
)
from kimi_bridge.interactions import (
    ApprovalRequest,
    MultipleChoiceAnswer,
    MultipleChoiceWithOtherAnswer,
    OtherAnswer,
    Question,
    QuestionOption,
    QuestionRequest,
    SingleChoiceAnswer,
    SkippedAnswer,
)
from kimi_bridge.kimi_server import (
    GoalBudget,
    GoalInfo,
    InteractionResolution,
    InteractionUpdate,
    InteractionSnapshot,
    KimiServerAPIError,
    KimiServerAuthenticationError,
    KimiServerClient,
    KimiServerError,
    KimiServerOperationError,
    KimiServerProtocolError,
    KimiServerStartupError,
    KimiServerTransportError,
    KimiServerSupervisor,
    ModelInfo,
    PromptContent,
    PromptDelivery,
    PromptMedia,
    SecondaryModelConfig,
    ServerConnection,
    SessionArchiveUncertain,
    SessionNotice,
    SessionStatus,
    SessionUsage,
    SkillInfo,
    TaskInfo,
    ToolInfo,
    parse_server_startup_line,
    session_notice_from_event,
)


KIMI_CODE_HELP = """Usage: kimi [options] [command]
The Starting Point for Next-Gen Agents
web [options]  Run the local Kimi server and open the web UI.
doctor  Validate Kimi Code configuration files.
migrate  Migrate data from a legacy kimi-cli installation into kimi-code.
"""
KIMI_WEB_HELP = """Usage: kimi web [options]
--no-open
--host <host>
--port <port>
"""

LEGACY_KIMI_CLI_HELP = """Usage: kimi [OPTIONS] COMMAND [ARGS]...
Kimi, your next CLI agent.
--mcp-config-file PATH
Documentation: https://moonshotai.github.io/kimi-cli/
"""


def _interaction_frame(kind: str, *, ended: bool = False) -> dict[str, Any]:
    return {
        "type": "interaction",
        "session_id": "session-1",
        "agent_id": "main",
        "event_created_at": "2026-10-30T00:00:00Z",
        "interaction_id": "interaction-1",
        "kind": kind,
        "status": "ended" if ended else "pending",
        **(
            {"outcome": "approved" if kind == "approval" else "answered"}
            if ended
            else {}
        ),
        "request": (
            {
                "tool_name": "Shell",
                "action": "Run command",
                "tool_input_display": {"command": "pwd"},
            }
            if kind == "approval"
            else {
                "questions": [
                    {
                        "id": "q1",
                        "question": "Pick one",
                        "header": "Choice",
                        "body": "Details",
                        "options": [
                            {"id": "one", "label": "One", "description": "First"},
                            {"id": "two", "label": "Two"},
                        ],
                        "allow_other": True,
                        "other_label": "Custom",
                        "multi_select": False,
                    }
                ],
            }
        ),
    }


@pytest.mark.parametrize("kind", ["approval", "question"])
@pytest.mark.parametrize("before_ack", [False, True])
async def test_interaction_frames_are_semantic_and_do_not_advance_cursor(
    kind, before_ack
) -> None:
    ack = {
        "accepted": ["session-1"],
        "not_found": [],
        "resync_required": [],
        "cursors": {},
    }
    first = FakeWebSocket(
        subscribe_payload=ack,
        events=[
            _ops_frame(
                [
                    {
                        "op": "turn.upsert",
                        "turn": {
                            "kind": "turn",
                            "turnId": "t0",
                            "ordinal": 0,
                            "state": "running",
                        },
                    }
                ],
                4,
            ),
            _interaction_frame(kind),
            _interaction_frame(kind, ended=True),
        ],
        events_before_ack=before_ack,
        disconnect_after_events=True,
    )
    second = FakeWebSocket(subscribe_payload=ack)
    http = FakeHttpClient(
        [],
        pending_snapshots=[["interaction-1"], []],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect([first, second]),
        reconnect_initial_backoff=0,
    )
    stream = client.subscribe_events("session-1")
    try:
        assert await anext(stream) == InteractionSnapshot(
            "session-1", frozenset({"interaction-1"})
        )
        assert (await anext(stream))["seq"] == 4
        event = await anext(stream)
        assert isinstance(event, InteractionUpdate)
        expected = (
            ApprovalRequest(
                "interaction-1", "session-1", "Shell", "Run command", {"command": "pwd"}
            )
            if kind == "approval"
            else QuestionRequest(
                "interaction-1",
                "session-1",
                (
                    Question(
                        "q1",
                        "Pick one",
                        (
                            QuestionOption("one", "One", "First"),
                            QuestionOption("two", "Two"),
                        ),
                        header="Choice",
                        body="Details",
                        allow_other=True,
                        other_label="Custom",
                    ),
                ),
            )
        )
        assert event.request == expected
        ended = await anext(stream)
        assert ended == InteractionUpdate(
            "session-1",
            "interaction-1",
            None,
            "approved" if kind == "approval" else "answered",
        )
        assert await asyncio.wait_for(anext(stream), 1) == InteractionSnapshot(
            "session-1", frozenset()
        )
        subscribe = next(
            frame for frame in second.sent if frame["type"] == "subscribe_v2"
        )
        assert subscribe["payload"]["transcript_since"] == {"main": 4}
        transcript_requests = [
            request for request in http.requests if request[1].endswith("/transcript")
        ]
        assert len(transcript_requests) == 2
        assert transcript_requests[0][2]["params"] == {
            "agent_id": "main",
            "page_size": 1,
        }
    finally:
        await stream.aclose()
        await client.aclose()


async def test_interactions_include_subagents_but_filter_other_sessions() -> None:
    frame = _interaction_frame("approval")
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        events=[
            dict(frame, session_id="other"),
            dict(frame, agent_id="child", interaction_id="child-request"),
            frame,
        ],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient(
            [_envelope({"busy": False})],
            pending_snapshots=[["interaction-1", "child-request"]],
        ),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    stream = client.subscribe_events("session-1")
    try:
        assert isinstance(await anext(stream), InteractionSnapshot)
        event = await anext(stream)
        assert isinstance(event, InteractionUpdate)
        assert event.session_id == "session-1"
        assert event.id == "child-request"
        assert event.request.session_id == "session-1"
        assert (await anext(stream)).id == "interaction-1"
    finally:
        await stream.aclose()
        await client.aclose()


@pytest.mark.parametrize("failed_request", [1, 2])
async def test_transcript_transport_failure_reconnects(failed_request) -> None:
    class FailingHttp(FakeHttpClient):
        transcript_requests = 0

        async def request(self, method, url, **kwargs):
            if url.endswith("/transcript"):
                self.transcript_requests += 1
                if self.transcript_requests == failed_request:
                    raise httpx.ConnectError("transient transcript failure")
            return await super().request(method, url, **kwargs)

    http = FailingHttp([_envelope({"busy": False})] * (failed_request + 1))
    sockets = [
        FakeWebSocket(
            subscribe_payload={"accepted": ["session-1"]},
            disconnect_after_events=True,
        )
        for _ in range(failed_request)
    ]
    sockets.append(
        FakeWebSocket(
            subscribe_payload={"accepted": ["session-1"]},
            events=[
                _ops_frame(
                    [
                        {
                            "op": "turn.upsert",
                            "turn": {
                                "kind": "turn",
                                "turnId": "t0",
                                "ordinal": 0,
                                "state": "running",
                            },
                        }
                    ],
                    2,
                )
            ],
        )
    )
    client = KimiServerClient(
        "http://localhost:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect(sockets),
        reconnect_initial_backoff=0,
    )
    stream = client.subscribe_events("session-1")
    try:
        async with asyncio.timeout(1):
            for _ in range(failed_request):
                assert isinstance(await anext(stream), InteractionSnapshot)
            event = await anext(stream)
            assert event["payload"]["type"] == "turn.started"
        assert http.transcript_requests == failed_request + 1
    finally:
        await stream.aclose()
        await client.aclose()


async def test_snapshot_filters_resolved_interactions_buffered_before_ack() -> None:
    pending = _interaction_frame("approval")
    fresh = dict(pending, interaction_id="still-pending")
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        events=[pending, _interaction_frame("approval", ended=True), fresh],
        events_before_ack=True,
    )
    client = KimiServerClient(
        "http://localhost:43123",
        "token",
        http_client=FakeHttpClient(
            [_envelope({"busy": False})],
            pending_snapshots=[["still-pending"]],
        ),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    stream = client.subscribe_events("session-1")
    try:
        assert await anext(stream) == InteractionSnapshot(
            "session-1", frozenset({"still-pending"})
        )
        assert await anext(stream) == InteractionUpdate(
            "session-1", "interaction-1", None, "approved"
        )
        assert (await anext(stream)).id == "still-pending"
    finally:
        await stream.aclose()
        await client.aclose()


async def test_invalid_transcript_is_a_protocol_failure_not_a_reconnect() -> None:
    http = FakeHttpClient(
        [_envelope({"busy": False})],
        transcript_snapshots=[{"agents": [], "pending_interactions": "invalid"}],
    )
    client = KimiServerClient(
        "http://localhost:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect(
            [FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})]
        ),
    )
    stream = client.subscribe_events("session-1")
    try:
        with pytest.raises(KimiServerProtocolError):
            await anext(stream)
        assert len([r for r in http.requests if r[1].endswith("/transcript")]) == 1
    finally:
        await stream.aclose()
        await client.aclose()


async def test_pending_frames_after_ack_are_checked_without_dropping_new_work() -> None:
    stale = _interaction_frame("approval")
    fresh = dict(stale, interaction_id="new-request", agent_id="child")
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        events=[stale, fresh],
    )
    http = FakeHttpClient(
        [_envelope({"busy": False})], pending_snapshots=[[], [], ["new-request"]]
    )
    client = KimiServerClient(
        "http://localhost:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect([socket]),
    )
    stream = client.subscribe_events("session-1")
    try:
        assert await anext(stream) == InteractionSnapshot("session-1", frozenset())
        event = await asyncio.wait_for(anext(stream), 1)
        assert isinstance(event, InteractionUpdate)
        assert event.id == "new-request"
        assert [
            request[2]["params"]["agent_id"]
            for request in http.requests
            if request[1].endswith("/transcript")
        ] == ["main", "main", "child"]
    finally:
        await stream.aclose()
        await client.aclose()


async def test_reconnect_reconciles_pending_interactions_from_all_agents() -> None:
    roster = [{"agentId": "main", "type": "main"}, {"agentId": "child", "type": "sub"}]
    http = FakeHttpClient(
        [_envelope({"busy": False}), _envelope({"busy": False})],
        transcript_snapshots=[
            {"agents": roster, "pending_interactions": ["main-request"]},
            {"agents": roster, "pending_interactions": ["child-request"]},
            {"agents": roster, "pending_interactions": []},
            {"agents": roster, "pending_interactions": ["child-request"]},
        ],
    )
    sockets = [
        FakeWebSocket(
            subscribe_payload={"accepted": ["session-1"]},
            disconnect_after_events=True,
        ),
        FakeWebSocket(subscribe_payload={"accepted": ["session-1"]}),
    ]
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect(sockets),
        reconnect_initial_backoff=0,
    )
    stream = client.subscribe_events("session-1")
    try:
        assert await anext(stream) == InteractionSnapshot(
            "session-1", frozenset({"main-request", "child-request"})
        )
        assert await asyncio.wait_for(anext(stream), 1) == InteractionSnapshot(
            "session-1", frozenset({"child-request"})
        )
        assert [
            request[2]["params"]["agent_id"]
            for request in http.requests
            if request[1].endswith("/transcript")
        ] == ["main", "child", "main", "child"]
    finally:
        await stream.aclose()
        await client.aclose()


def _envelope(data: Any, *, code: int = 0, msg: str = "success") -> dict[str, Any]:
    return {"code": code, "msg": msg, "data": data, "request_id": "req-1"}


def _profile_payload(
    *,
    session_id: str = "session-1",
    title: str = "Test session",
    model: str = "kimi-code/k3",
    thinking: str = "high",
    permission_mode: str = "manual",
    plan_mode: bool = False,
) -> dict[str, Any]:
    return {
        "id": session_id,
        "title": title,
        "busy": False,
        "pending_interaction": "none",
        "metadata": {"cwd": "/tmp/workspace"},
        "agent_config": {
            "model": model,
            "thinking": thinking,
            "permission_mode": permission_mode,
            "plan_mode": plan_mode,
        },
        "usage": {
            "input_tokens": 10,
            "output_tokens": 20,
            "cache_read_tokens": 3,
            "cache_creation_tokens": 4,
            "total_cost_usd": 0.0125,
            "context_tokens": 30,
            "context_limit": 100,
            "turn_count": 2,
        },
    }


async def _session_events(client: KimiServerClient, session_id: str):
    stream = client.subscribe_events(session_id)
    try:
        async for event in stream:
            if isinstance(event, dict):
                yield event
    finally:
        await stream.aclose()


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    def json(self) -> dict[str, Any]:
        return self._payload


class FakeHttpClient:
    def __init__(
        self,
        responses: Iterable[dict[str, Any]],
        *,
        actions: list[str] | None = None,
        pending_snapshots: Iterable[list[str]] | None = None,
        transcript_snapshots: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self._pending_snapshots = (
            iter(pending_snapshots) if pending_snapshots is not None else None
        )
        self._transcript_snapshots = (
            iter(transcript_snapshots) if transcript_snapshots is not None else None
        )
        self._responses = iter(responses)
        self._actions = actions
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        if self._actions is not None:
            self._actions.append(f"http:{method}:{url}")
        self.requests.append((method, url, kwargs))
        if method == "GET" and (
            url.endswith("/transcript") or url.endswith("/transcript/ops")
        ):
            if self._transcript_snapshots is not None:
                return FakeResponse(_envelope(next(self._transcript_snapshots)))
            return FakeResponse(
                _envelope(
                    {
                        "items": [],
                        "attachments": [],
                        "prompts": [],
                        "has_more": False,
                        "agents": [{"agentId": "main", "type": "main"}],
                        "pending_interactions": next(self._pending_snapshots)
                        if self._pending_snapshots is not None
                        else [],
                        "seq": 1,
                    }
                )
            )
        return FakeResponse(next(self._responses))


class FakeWebSocket:
    def __init__(
        self,
        *,
        subscribe_payload: dict[str, Any],
        subscribe_payloads: Iterable[dict[str, Any]] | None = None,
        events: Iterable[dict[str, Any]] = (),
        events_before_ack: bool = False,
        disconnect_after_events: bool = False,
        mismatched_subscribe_ack: bool = False,
        agent_burst: bool = True,
        reset_seq: int = 1,
        reset_on_resume: bool = False,
        tasks: Iterable[dict[str, Any]] = (),
        task_responses: Iterable[dict[str, Any]] | None = None,
    ) -> None:
        self._subscribe_payload = subscribe_payload
        self._subscribe_payloads = (
            iter(subscribe_payloads) if subscribe_payloads is not None else None
        )
        self._events = list(events)
        self._events_before_ack = events_before_ack
        self._disconnect_after_events = disconnect_after_events
        self._mismatched_subscribe_ack = mismatched_subscribe_ack
        self._agent_burst = agent_burst
        self._reset_seq = reset_seq
        self._reset_on_resume = reset_on_resume
        self._tasks = list(tasks)
        self._task_responses = (
            list(task_responses) if task_responses is not None else None
        )
        self._incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()
        self._incoming.put_nowait(
            json.dumps(
                {
                    "type": "server_hello",
                    "timestamp": "now",
                    "payload": {
                        "ws_connection_id": "conn-1",
                        "protocol_version": 2,
                        "heartbeat_ms": 10000,
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
                        "payload": {},
                    }
                )
            )
        elif frame["type"] == "subscribe_v2":
            if self._subscribe_payloads is not None:
                self._subscribe_payload = next(self._subscribe_payloads)
            session_id = frame.get("payload", {}).get("session_id", "session-1")
            resume = "transcript_since" in frame.get("payload", {})
            if self._agent_burst:
                for burst_frame in self._agent_burst_frames(
                    session_id, include_reset=(not resume or self._reset_on_resume)
                ):
                    self._incoming.put_nowait(json.dumps(burst_frame))
            if self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))
            self._incoming.put_nowait(
                json.dumps(
                    {
                        "type": "ack",
                        "id": (
                            "mismatched-request"
                            if self._mismatched_subscribe_ack
                            else frame["id"]
                        ),
                        "code": 0,
                        "msg": "success",
                        "payload": self._subscribe_payload,
                    }
                )
            )
            if not self._events_before_ack:
                for event in self._events:
                    self._incoming.put_nowait(json.dumps(event))
            if self._disconnect_after_events:
                self._incoming.put_nowait(OSError("connection lost"))
        elif frame["type"] in ("task.watch", "task.stop", "task.background"):
            for extra in getattr(self, "_task_output_frames_before_response", ()):
                self._incoming.put_nowait(json.dumps(extra))
            if self._task_responses is not None:
                response = dict(self._task_responses.pop(0))
            else:
                response = {
                    "type": "response",
                    "code": 0,
                    "msg": "success",
                    "task": _task_payload(
                        frame.get("task_id", "task-1"),
                        status="ended",
                        outcome="completed",
                    ),
                }
            response["request_id"] = frame["request_id"]
            self._incoming.put_nowait(json.dumps(response))
            for extra in getattr(self, "_task_output_frames_after_response", ()):
                self._incoming.put_nowait(json.dumps(extra))

    def _agent_burst_frames(
        self, session_id: str, *, include_reset: bool = True
    ) -> list[dict[str, Any]]:
        frames = [
            {
                "type": "task.list",
                "session_id": session_id,
                "agent_id": "main",
                "tasks": self._tasks,
            },
            {
                "type": "agent",
                "session_id": session_id,
                "agent_id": "main",
                "status": "idle",
                "permission_mode": "yolo",
                "plan_mode": False,
                "swarm_mode": False,
                "tower_mode": False,
                "event_created_at": "now",
            },
            {
                "type": "usage",
                "session_id": session_id,
                "agent_id": "main",
                "context_tokens": 100,
                "max_context_tokens": 1000,
                "event_created_at": "now",
            },
            {"type": "goal", "session_id": session_id, "agent_id": "main"},
            {"type": "todo", "session_id": session_id, "agent_id": "main", "items": []},
        ]
        if include_reset:
            frames.append(
                {
                    "type": "transcript.reset",
                    "session_id": session_id,
                    "payload": {
                        "type": "transcript.reset",
                        "agent_id": "main",
                        "snapshot": {
                            "items": list(
                                getattr(self, "_reset_snapshot_items", [])
                            ),
                            "interactions": [],
                            "attachments": [],
                            "prompts": [],
                            "hasMoreOlder": False,
                        },
                        "has_more_older": False,
                        "seq": self._reset_seq,
                    },
                }
            )
        return frames

    async def recv(self) -> str:
        incoming = await self._incoming.get()
        if isinstance(incoming, BaseException):
            raise incoming
        return incoming

    async def close(self) -> None:
        self.closed = True


class FakeWebSocketConnect:
    def __init__(
        self,
        sockets: Iterable[FakeWebSocket],
        *,
        actions: list[str] | None = None,
    ) -> None:
        self._sockets = iter(sockets)
        self._actions = actions
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> FakeWebSocket:
        if self._actions is not None:
            self._actions.append(f"ws:{url}")
        self.calls.append((url, kwargs))
        return next(self._sockets)


class FakeCompletedProcess:
    def __init__(self, output: str, *, returncode: int = 0) -> None:
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_data(output.encode())
        self.stdout.feed_eof()
        self.returncode = returncode

    async def wait(self) -> int:
        return self.returncode

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


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
    def __init__(self, processes: Iterable[Any]) -> None:
        self._processes = iter(processes)
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        return next(self._processes)


class FakeSupervisor:
    def __init__(self, connections: Iterable[ServerConnection]) -> None:
        self._connections = iter(connections)
        self.restart_calls = 0
        self.restart_error: KimiServerError | None = None

    async def wait_until_ready(self) -> ServerConnection:
        return next(self._connections)

    async def restart(self) -> ServerConnection:
        self.restart_calls += 1
        if self.restart_error is not None:
            raise self.restart_error
        return next(self._connections)


class FakeVersionedSupervisor(FakeSupervisor):
    def __init__(
        self, connections: Iterable[ServerConnection], *, version: str
    ) -> None:
        super().__init__(connections)
        self.executable_identity = KimiExecutableIdentity(
            product=KimiProduct.KIMI_CODE,
            version=version,
        )


def _task_payload(
    task_id: str,
    *,
    status: str = "running",
    outcome: str | None = None,
    kind: str = "shell",
    description: str = "sleep 5",
    command: str | None = "sleep 5",
) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": task_id,
        "kind": kind,
        "status": status,
        "detached": True,
        "description": description,
        "command": command,
        "started_at": "2026-01-01T00:00:00Z",
    }
    if outcome is not None:
        task["outcome"] = outcome
        task["ended_at"] = "2026-01-01T00:00:01Z"
        task["result_summary"] = "done"
    return task


def _ops_frame(ops: list[dict[str, Any]], seq: int) -> dict[str, Any]:
    return {
        "type": "transcript.ops",
        "session_id": "session-1",
        "payload": {"type": "transcript.ops", "agent_id": "main", "ops": ops, "seq": seq},
    }


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


def test_windows_spawn_target_resolves_path_shims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kimi_bridge.kimi_server import supervisor as supervisor_module

    spawn_target = supervisor_module._spawn_target
    monkeypatch.setattr(
        supervisor_module.shutil, "which", lambda _name: r"C:\npm\kimi.CMD"
    )
    assert spawn_target("kimi", "win32") == r"C:\npm\kimi.CMD"
    assert spawn_target("kimi", "linux") == "kimi"
    assert spawn_target("kimi", "darwin") == "kimi"

    monkeypatch.setattr(supervisor_module.shutil, "which", lambda _name: None)
    assert spawn_target("kimi", "win32") == "kimi"


async def test_rest_methods_use_snapshotted_paths_and_shapes(tmp_path: Any) -> None:
    http = FakeHttpClient(
        [
            _envelope({"server_version": "2.5.1"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"default_model": "kimi-code/k3"}),
            _envelope({"id": "session-1"}),
            _envelope(_profile_payload()),
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
        ],
        transcript_snapshots=[
            {
                "agent_id": "main",
                "seq": 4,
                "items": [],
                "attachments": [],
                "prompts": [],
                "has_more": False,
                "pending_interactions": [],
                "agents": [{"agentId": "main", "type": "main"}],
            }
        ],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=FakeWebSocketConnect([]),
    )

    assert await client.check_server_version() == "2.5.1"
    assert await client.get_config() == {"default_model": "kimi-code/k3"}
    assert await client.get_default_model() == "kimi-code/k3"
    assert (
        await client.create_session(
            str(tmp_path), title="Test session", permission_mode="auto"
        )
        == "session-1"
    )
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
        "turn_ids": [],
        "messages": {"items": [], "has_more": False},
        "in_flight_turn": None,
    }

    assert [request[0:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/api/v1/meta"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("GET", "http://127.0.0.1:43123/api/v1/config"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/profile"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-1:abort",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/transcript"),
    ]
    assert http.requests[3][2]["json"] == {
        "metadata": {"cwd": str(tmp_path.resolve())},
        "title": "Test session",
    }
    assert http.requests[4][2]["json"] == {"agent_config": {"permission_mode": "auto"}}
    assert http.requests[6][2]["json"] == {
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
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerAPIError) as caught:
        await client.get_session("missing")

    assert caught.value.code == 40401


async def test_snapshot_reads_the_transcript_without_disturbing_the_subscription() -> None:
    live = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        events=[
            _ops_frame(
                [
                    {
                        "op": "turn.upsert",
                        "turn": {
                            "kind": "turn",
                            "turnId": "t0",
                            "ordinal": 0,
                            "state": "running",
                        },
                    }
                ],
                2,
            )
        ],
    )
    transcript = {
        "agent_id": "main",
        "seq": 987,
        "items": [],
        "attachments": [],
        "prompts": [],
        "has_more": False,
        "pending_interactions": [],
        "agents": [{"agentId": "main", "type": "main"}],
    }
    http = FakeHttpClient(
        [],
        transcript_snapshots=[dict(transcript), dict(transcript)],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http,
        ws_connect=FakeWebSocketConnect([live]),
    )
    events = _session_events(client, "session-1")
    assert (await anext(events))["payload"]["type"] == "turn.started"
    snapshot = await asyncio.wait_for(client.get_snapshot("session-1"), 1)
    assert snapshot["as_of_seq"] == 987
    assert snapshot["messages"]["items"] == []
    assert client._active_ws[("session-1", "main")] is live
    assert not live.closed
    assert http.requests[-1][2]["params"] == {"agent_id": "main", "page_size": 100}
    assert len([r for r in http.requests if r[1].endswith("/transcript")]) == 2
    await events.aclose()


async def test_snapshot_maps_running_turns_without_a_websocket() -> None:
    http = FakeHttpClient(
        [],
        transcript_snapshots=[
            {
                "agent_id": "main",
                "seq": 500,
                "items": [
                    {
                        "kind": "turn",
                        "ordinal": 2,
                        "state": "running",
                        "steps": [
                            {
                                "frames": [
                                    {
                                        "kind": "text",
                                        "role": "assistant",
                                        "text": "Working",
                                    }
                                ]
                            }
                        ],
                    }
                ],
                "attachments": [],
                "prompts": [],
                "has_more": False,
                "pending_interactions": [],
            }
        ],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http,
        ws_connect=FakeWebSocketConnect([]),
    )
    snapshot = await client.get_snapshot("session-1")
    assert snapshot["as_of_seq"] == 500
    assert snapshot["in_flight_turn"]["assistant_text"] == "Working"
    assert snapshot["in_flight_turn"]["turn_id"] == 2


async def test_snapshot_transport_failure_is_a_transport_error() -> None:
    class FailingHttp(FakeHttpClient):
        async def request(self, method, url, **kwargs):
            raise httpx.ConnectError("transient transcript failure")

    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=FailingHttp([]),
        ws_connect=FakeWebSocketConnect([]),
    )
    with pytest.raises(KimiServerTransportError):
        await client.get_snapshot("session-1")


async def test_client_restart_requires_and_delegates_to_managed_supervisor() -> None:
    fixed = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
    )
    with pytest.raises(KimiServerError, match="requires a managed"):
        await fixed.restart_server()

    unavailable = KimiServerClient(
        supervisor=KimiServerSupervisor(),
        http_client=FakeHttpClient([]),
    )
    with pytest.raises(KimiServerError, match="supervisor is not running"):
        await unavailable.restart_server()

    connection = ServerConnection(
        base_url="http://127.0.0.1:43123",
        port=43123,
        generation=2,
        token="token-2",
    )
    supervisor = FakeSupervisor([connection])
    managed = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=FakeHttpClient([]),
    )
    managed._agent_frames["session-1"] = {"status": "idle"}
    managed._usage_frames["session-1"] = {"context_tokens": 1}

    assert await managed.restart_server() == connection
    assert supervisor.restart_calls == 1
    assert managed._agent_frames == {}
    assert managed._usage_frames == {}


async def test_client_restart_preserves_usage_when_supervisor_restart_fails() -> None:
    supervisor = FakeSupervisor([])
    supervisor.restart_error = KimiServerError("restart failed")
    client = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=FakeHttpClient([]),
    )
    client._agent_frames["session-1"] = {"status": "idle"}
    client._usage_frames["session-1"] = {"context_tokens": 1}

    with pytest.raises(KimiServerError, match="restart failed"):
        await client.restart_server()

    assert client._agent_frames == {"session-1": {"status": "idle"}}
    assert client._usage_frames == {"session-1": {"context_tokens": 1}}


async def test_session_model_resolution_uses_bound_model_and_live_catalog() -> None:
    status = {
        "id": "session-1",
        "busy": False,
        "metadata": {"cwd": "/tmp"},
        "agent_config": {
            "model": "kimi-code/vision",
            "thinking": "off",
            "permission_mode": "manual",
            "plan_mode": False,
            "swarm_mode": False,
        },
        "usage": {"context_tokens": 0, "context_limit": 262_144},
    }
    model = {
        "provider": "kimi-code",
        "model": "kimi-code/vision",
        "display_name": "Vision",
        "max_context_size": 262_144,
        "capabilities": ["image_in", "video_in"],
    }
    http = FakeHttpClient(
        [
            _envelope(status),
            _envelope({"items": [model]}),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
    )

    resolved = await client.get_session_model("session-1")

    assert resolved.alias == "kimi-code/vision"
    assert resolved.capabilities == ("image_in", "video_in")


async def test_session_status_reads_the_stored_session_object() -> None:
    status = {
        "id": "session-1",
        "busy": False,
        "metadata": {"cwd": "/tmp"},
        "agent_config": {
            "model": "removed-model",
            "thinking": "off",
            "permission_mode": "manual",
            "plan_mode": False,
            "swarm_mode": False,
        },
        "usage": {"context_tokens": 0, "context_limit": None},
    }
    http = FakeHttpClient([_envelope(status), _envelope(status), _envelope(status)])
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
    )

    assert await client.get_session_status("session-1") == SessionStatus(
        busy=False,
        model="removed-model",
        thinking_effort="off",
        permission_mode="manual",
        plan_mode=False,
        swarm_mode=False,
        context_tokens=0,
        context_limit=None,
    )
    assert await client.get_session_usage("session-1") == SessionUsage(
        None, None, None, None, 0, None
    )
    client._usage_frames["session-1"] = {
        "type": "usage",
        "session_id": "session-1",
        "agent_id": "main",
        "context_tokens": 0,
        "max_context_tokens": None,
    }
    assert await client.get_session_usage("session-1") == SessionUsage(
        None, None, None, None, 0, None
    )


async def test_live_agent_settings_and_context_expire_with_the_subscription() -> None:
    class LiveStateSocket(FakeWebSocket):
        def _agent_burst_frames(self, session_id, *, include_reset=True):
            frames = super()._agent_burst_frames(
                session_id, include_reset=include_reset
            )
            for frame in frames:
                if frame["type"] == "agent":
                    frame.update(
                        model="kimi-code/live",
                        thinking="max",
                        permission_mode="auto",
                        plan_mode=True,
                        swarm_mode=True,
                    )
            return frames

    stored = {
        "id": "session-1",
        "busy": False,
        "agent_config": {"model": "kimi-code/stored"},
        "usage": {"input_tokens": 500, "output_tokens": 100, "context_tokens": 10},
    }
    socket = LiveStateSocket(subscribe_payload={"accepted": ["session-1"]})
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([_envelope(stored)] * 4),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        assert (
            await client.get_session_status("session-1")
        ).model == "kimi-code/stored"
        stream = client.subscribe_events("session-1")
        try:
            await anext(stream)
            status = await client.get_session_status("session-1")
            assert (status.model, status.thinking_effort, status.permission_mode) == (
                "kimi-code/live",
                "max",
                "auto",
            )
            assert status.plan_mode and status.swarm_mode
            assert (await client.get_session_usage("session-1")).input_tokens == 500
            assert status.context_tokens == 100
        finally:
            await stream.aclose()
        assert (
            await client.get_session_status("session-1")
        ).model == "kimi-code/stored"


async def test_large_prompt_media_early_disconnect_is_a_kimi_transport_error() -> None:
    async def disconnect_after_headers(
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        await reader.readuntil(b"\r\n\r\n")
        writer.transport.abort()

    server = await asyncio.start_server(disconnect_after_headers, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = KimiServerClient(f"http://127.0.0.1:{port}", "token-1")
    try:
        with pytest.raises(
            KimiServerTransportError,
            match=r"POST /files failed",
        ):
            await client.submit_prompt(
                "session-1",
                PromptContent(
                    media=(
                        PromptMedia(
                            "image",
                            b"x" * 2_500_000,
                            "large.png",
                            "image/png",
                        ),
                    )
                ),
            )
    finally:
        await client.aclose()
        server.close()
        await server.wait_closed()


async def test_abort_session_clears_prompt_queue_before_stopping_main_turn() -> None:
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "active": {"prompt_id": "prompt-active"},
                    "queued": [
                        {"prompt_id": "prompt-next"},
                        {"prompt_id": "prompt-last"},
                    ],
                }
            ),
            _envelope({"aborted": True}),
            _envelope({"aborted": True}),
            _envelope({"aborted": True}),
            _envelope({"aborted": True}),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    assert await client.abort_session("session-1") is True
    assert [request[0:2] for request in http.requests] == [
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-next:abort",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-last:abort",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts/prompt-active:abort",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:abort",
        ),
    ]
    assert http.requests[-1][2]["json"] == {}


async def test_abort_session_stops_promptless_main_turn() -> None:
    http = FakeHttpClient(
        [
            _envelope({"active": None, "queued": []}),
            _envelope({"aborted": True}),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    assert await client.abort_session("session-1") is True
    assert [request[0:2] for request in http.requests] == [
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:abort",
        ),
    ]


async def test_control_and_inspection_methods_use_public_v1_shapes() -> None:
    model = {
        "provider": "kimi-code",
        "model": "kimi-code/k3",
        "display_name": "K3",
        "max_context_size": 262144,
        "capabilities": ["thinking"],
        "support_efforts": ["low", "high", "max"],
        "default_effort": "high",
    }
    status = {
        "id": "session-1",
        "busy": True,
        "metadata": {"cwd": "/tmp/workspace"},
        "agent_config": {
            "model": "kimi-code/k3",
            "thinking": "max",
            "permission_mode": "manual",
            "plan_mode": True,
            "swarm_mode": False,
        },
        "usage": {"context_tokens": 30, "context_limit": 100},
    }
    task = _task_payload("bash-1", description="Run checks", command="pytest")
    skill = {
        "name": "review/code",
        "description": "Review code",
        "path": "/skills/review/code",
        "source": "user",
        "type": "prompt",
        "disable_model_invocation": False,
    }
    tool = {
        "name": "mcp_search",
        "description": "Search",
        "input_schema": {},
        "source": "mcp",
        "mcp_server_id": "search-server",
    }
    updated = _profile_payload(
        title="Renamed",
        model="kimi-code/k3",
        thinking="max",
        plan_mode=True,
    )
    http = FakeHttpClient(
        [
            _envelope({"server_version": "2.5.1"}),
            _envelope({"items": [model]}),
            _envelope(_profile_payload()),
            _envelope(status),
            _envelope(status),
            _envelope(updated),
            _envelope({"skills": [skill]}),
            _envelope({"activated": True, "skill_name": "review/code"}),
            _envelope({"tools": [tool]}),
        ]
    )
    list_socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        tasks=[task],
    )
    watch_socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        task_responses=[
            {
                "type": "response",
                "request_id": "to-be-replaced",
                "code": 0,
                "msg": "success",
                "task": task,
            }
        ],
    )
    # The output drain ends once a non-task frame arrives after the response.
    watch_socket._task_output_frames_after_response = [
        {
            "type": "task.output",
            "session_id": "session-1",
            "agent_id": "main",
            "task_id": "bash-1",
            "offset": 0,
            "text": "tail",
        },
        {"type": "goal", "session_id": "session-1", "agent_id": "main"},
    ]
    stop_socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
        task_responses=[
            {
                "type": "response",
                "request_id": "to-be-replaced",
                "code": 0,
                "msg": "success",
                "task": _task_payload("bash-1", status="ended", outcome="killed"),
            }
        ],
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=FakeWebSocketConnect([list_socket, watch_socket, stop_socket]),
    )
    client._usage_frames["session-1"] = {
        "context_tokens": 30,
        "max_context_tokens": 100,
    }

    assert await client.get_server_version() == "2.5.1"
    assert await client.list_models() == [
        ModelInfo(
            alias="kimi-code/k3",
            provider="kimi-code",
            display_name="K3",
            max_context_size=262144,
            capabilities=("thinking",),
            support_efforts=("low", "high", "max"),
            default_effort="high",
        )
    ]
    assert (await client.get_session_profile("session-1")).title == "Test session"
    assert await client.get_session_status("session-1") == SessionStatus(
        busy=True,
        model="kimi-code/k3",
        thinking_effort="max",
        permission_mode="manual",
        plan_mode=True,
        swarm_mode=False,
        context_tokens=30,
        context_limit=100,
    )
    assert await client.get_session_usage("session-1") == SessionUsage(
        None, None, None, None, 30, 100
    )
    assert (
        await client.update_profile(
            "session-1",
            title="Renamed",
            model="kimi-code/k3",
            thinking="max",
            plan_mode=True,
        )
    ).title == "Renamed"
    assert await client.list_tasks("session-1", status="running") == [
        TaskInfo(
            id="bash-1",
            session_id="session-1",
            kind="bash",
            description="Run checks",
            status="running",
            command="pytest",
            created_at=task["started_at"],
            started_at=task["started_at"],
            output_preview=None,
            output_bytes=None,
        )
    ]
    watched = await client.get_task("session-1", "bash-1", output_bytes=8192)
    assert watched.output_preview == "tail"
    assert watched.output_bytes == 4
    assert await client.cancel_task("session-1", "bash-1")
    assert await client.list_skills("session-1") == [
        SkillInfo(
            name="review/code",
            description="Review code",
            source="user",
            path="/skills/review/code",
            kind="prompt",
            disable_model_invocation=False,
        )
    ]
    assert (
        await client.activate_skill("session-1", "review/code", args="focus tests")
        == "review/code"
    )
    assert await client.list_tools("session-1") == [
        ToolInfo("mcp_search", "Search", "mcp", "search-server")
    ]

    assert [request[0:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/api/v1/meta"),
        ("GET", "http://127.0.0.1:43123/api/v1/models"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/profile"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1"),
        ("POST", "http://127.0.0.1:43123/api/v1/sessions/session-1/profile"),
        ("GET", "http://127.0.0.1:43123/api/v1/sessions/session-1/skills"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/skills/review%2Fcode:activate",
        ),
        ("GET", "http://127.0.0.1:43123/api/v1/tools"),
    ]
    assert http.requests[5][2]["json"] == {
        "title": "Renamed",
        "agent_config": {
            "model": "kimi-code/k3",
            "thinking": "max",
            "plan_mode": True,
        },
    }
    assert http.requests[7][2]["json"] == {"args": "focus tests"}
    assert http.requests[8][2]["params"] == {"session_id": "session-1"}

    list_subscribe = next(
        frame for frame in list_socket.sent if frame["type"] == "subscribe_v2"
    )
    assert list_subscribe["payload"]["transcript"] == {"*": "turn"}
    watch_request = next(
        frame for frame in watch_socket.sent if frame["type"] == "task.watch"
    )
    assert watch_request["session_id"] == "session-1"
    assert watch_request["agent_id"] == "main"
    assert watch_request["task_id"] == "bash-1"
    stop_request = next(
        frame for frame in stop_socket.sent if frame["type"] == "task.stop"
    )
    assert stop_request["task_id"] == "bash-1"


async def test_secondary_model_config_supports_legacy_and_pool_shapes() -> None:
    http = FakeHttpClient(
        [
            _envelope({}),
            _envelope({"secondary_model": {"model": "kimi-code/legacy"}}),
            _envelope(
                {
                    "secondary_model": {
                        "defaultModel": "kimi-code/fast",
                        "models": {
                            "kimi-code/k3": "hard tasks",
                            "kimi-code/fast": "fast tasks",
                        },
                        "force": False,
                    }
                }
            ),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    assert await client.get_secondary_model() == SecondaryModelConfig(
        default_model=None, models=None, force=False
    )
    assert await client.get_secondary_model() == SecondaryModelConfig(
        default_model="kimi-code/legacy", models=None, force=False
    )
    assert await client.get_secondary_model() == SecondaryModelConfig(
        default_model="kimi-code/fast",
        models=(
            ("kimi-code/k3", "hard tasks"),
            ("kimi-code/fast", "fast tasks"),
        ),
        force=False,
    )


@pytest.mark.parametrize("force", [False, True])
async def test_set_secondary_model_extends_an_existing_model_pool(force: bool) -> None:
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "secondary_model": {
                        "defaultModel": "kimi-code/k3",
                        "force": force,
                        "models": {"kimi-code/k3": "hard tasks"},
                    }
                }
            ),
            _envelope(
                {
                    "secondary_model": {
                        "defaultModel": "kimi-code/fast",
                        "force": force,
                        "models": {
                            "kimi-code/k3": "hard tasks",
                            "kimi-code/fast": "",
                        },
                    }
                }
            ),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    updated = await client.set_secondary_model("kimi-code/fast")

    assert updated.default_model == "kimi-code/fast"
    assert updated.force is force
    assert http.requests[-1][0:2] == (
        "POST",
        "http://127.0.0.1:43123/api/v1/config",
    )
    assert http.requests[-1][2]["json"] == {
        "secondary_model": {
            "default_model": "kimi-code/fast",
            "models": {"kimi-code/fast": ""},
        }
    }


@pytest.mark.parametrize(
    ("current", "patch", "persisted"),
    [
        (
            {},
            {"default_model": "kimi-code/fast", "models": {"kimi-code/fast": "Fast tasks"}},
            {"defaultModel": "kimi-code/fast", "models": {"kimi-code/fast": "Fast tasks"}},
        ),
        (
            {"defaultModel": "kimi-code/k3"},
            {"models": {"kimi-code/fast": "Fast tasks", "kimi-code/k3": ""}},
            {"defaultModel": "kimi-code/k3", "models": {"kimi-code/fast": "Fast tasks", "kimi-code/k3": ""}},
        ),
        (
            {"defaultModel": "kimi-code/fast"},
            {"models": {"kimi-code/fast": "Fast tasks"}},
            {"defaultModel": "kimi-code/fast", "models": {"kimi-code/fast": "Fast tasks"}},
        ),
        (
            {"defaultModel": "kimi-code/k3", "models": {"kimi-code/k3": "Hard tasks", "kimi-code/fast": "Old hint"}},
            {"models": {"kimi-code/fast": "Fast tasks"}},
            {"defaultModel": "kimi-code/k3", "models": {"kimi-code/k3": "Hard tasks", "kimi-code/fast": "Fast tasks"}},
        ),
    ],
    ids=["first-entry", "implicit-default", "describe-default", "edit-entry"],
)
@pytest.mark.parametrize("force", [False, True])
async def test_set_secondary_model_entry_uses_merge_and_preserves_default(
    current: dict[str, Any], patch: dict[str, Any], persisted: dict[str, Any], force: bool,
) -> None:
    current = {**current, "force": force}
    persisted = {**persisted, "force": force}
    http = FakeHttpClient([
        _envelope({"secondary_model": current}),
        _envelope({"secondary_model": persisted}),
    ])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    updated = await client.set_secondary_model_entry("kimi-code/fast", "Fast tasks")

    assert [request[0] for request in http.requests] == ["GET", "POST"]
    assert http.requests[-1][1] == "http://127.0.0.1:43123/api/v1/config"
    assert http.requests[-1][2]["json"] == {"secondary_model": patch}
    assert updated.force is force
    assert updated.default_model == persisted["defaultModel"]
    assert dict(updated.models or ()) == persisted["models"]


async def test_secondary_model_pool_rejects_reserved_alias() -> None:
    http = FakeHttpClient([
        _envelope({"secondary_model": {"defaultModel": "kimi-code/k3", "force": True}}),
    ])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerOperationError):
        await client.set_secondary_model_entry("primary", "Fast tasks")

    assert all(request[0] == "GET" for request in http.requests)


@pytest.mark.parametrize(
    "persisted",
    [
        {"defaultModel": "kimi-code/k3", "models": {"kimi-code/fast": "Old hint"}},
        {"defaultModel": "kimi-code/fast", "models": {"kimi-code/fast": "Fast tasks"}},
    ],
)
async def test_secondary_model_entry_checks_persisted_description_and_default(
    persisted: dict[str, Any],
) -> None:
    http = FakeHttpClient([
        _envelope({"secondary_model": {"defaultModel": "kimi-code/k3"}}),
        _envelope({"secondary_model": persisted}),
    ])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerProtocolError):
        await client.set_secondary_model_entry("kimi-code/fast", "Fast tasks")


@pytest.mark.parametrize("rejected", [False, True])
async def test_reload_session_uses_native_action_and_preserves_usage(rejected: bool) -> None:
    http = FakeHttpClient([
        _envelope(None, code=40901, msg="Turn is running")
        if rejected else _envelope({"id": "session /1"})
    ])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)
    client._agent_frames["session /1"] = {"status": "idle"}

    if rejected:
        with pytest.raises(KimiServerAPIError) as exc_info:
            await client.reload_session("session /1")
        assert exc_info.value.code == 40901
    else:
        await client.reload_session("session /1")

    assert len(http.requests) == 1
    assert http.requests[0][:2] == (
        "POST", "http://127.0.0.1:43123/api/v1/sessions/session%20%2F1:reload"
    )
    assert http.requests[0][2]["json"] == {}
    assert client._agent_frames == {"session /1": {"status": "idle"}}


async def test_interaction_profile_steer_and_media_methods_use_spec_shapes() -> None:
    http = FakeHttpClient(
        [
            _envelope(
                {
                    "id": "file-image",
                    "name": "photo.png",
                    "media_type": "image/png",
                    "size": 5,
                    "created_at": "now",
                }
            ),
            _envelope(
                {
                    "id": "file-video",
                    "name": "clip.mp4",
                    "media_type": "video/mp4",
                    "size": 5,
                    "created_at": "now",
                }
            ),
            _envelope({"prompt_id": "prompt-1", "status": "queued"}),
            _envelope({"steered": True, "prompt_ids": ["prompt-1"]}),
            _envelope(_profile_payload(permission_mode="yolo")),
            _envelope({"outcome": "approved", "ended_at": "now"}),
            _envelope({"outcome": "answered", "ended_at": "now"}),
            _envelope({"outcome": "dismissed", "ended_at": "now"}),
        ]
    )
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)
    content = PromptContent(
        text="look",
        media=(
            PromptMedia("image", b"image", "photo.png", "image/png"),
            PromptMedia("video", b"video", "clip.mp4", "video/mp4"),
        ),
    )

    await client.submit_prompt(
        "session-1",
        content,
        model="kimi-code/k3",
        permission_mode="manual",
        delivery=PromptDelivery.STEER_IF_ACTIVE,
    )
    assert (
        await client.update_profile("session-1", permission_mode="yolo")
    ).session_id == "session-1"
    assert (
        await client.resolve_approval("session-1", "approval-1", "approved")
        is InteractionResolution.APPLIED
    )
    answers = (
        SingleChoiceAnswer("q1", "one"),
        MultipleChoiceAnswer("q2", ("x", "y")),
        OtherAnswer("q3", "custom"),
        MultipleChoiceWithOtherAnswer("q4", ("left",), "another"),
        SkippedAnswer("q5"),
    )
    assert (
        await client.resolve_question("session-1", "question-1", answers)
        is InteractionResolution.APPLIED
    )
    assert (
        await client.dismiss_question("session-1", "question-1")
        is InteractionResolution.APPLIED
    )

    assert [request[0:2] for request in http.requests] == [
        ("POST", "http://127.0.0.1:43123/api/v1/files"),
        ("POST", "http://127.0.0.1:43123/api/v1/files"),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/prompts:steer",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/interactions/approval-1",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/interactions/question-1",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/interactions/question-1",
        ),
    ]
    assert http.requests[0][2]["data"] == {"name": "photo.png"}
    assert http.requests[0][2]["files"] == {
        "file": ("photo.png", b"image", "image/png")
    }
    assert http.requests[1][2]["data"] == {"name": "clip.mp4"}
    assert http.requests[1][2]["files"] == {
        "file": ("clip.mp4", b"video", "video/mp4")
    }
    assert http.requests[2][2]["json"] == {
        "content": [
            {"type": "text", "text": "look"},
            {
                "type": "image",
                "source": {"kind": "file", "file_id": "file-image"},
            },
            {
                "type": "video",
                "source": {"kind": "file", "file_id": "file-video"},
            },
        ],
        "model": "kimi-code/k3",
        "permission_mode": "manual",
    }
    assert http.requests[3][2]["json"] == {"prompt_ids": ["prompt-1"]}
    assert http.requests[4][2]["json"] == {"agent_config": {"permission_mode": "yolo"}}
    assert http.requests[5][2]["json"] == {"kind": "approval", "decision": "approved"}
    assert http.requests[6][2]["json"] == {
        "kind": "question",
        "answers": {
            "q1": {"kind": "single", "option_id": "one"},
            "q2": {"kind": "multi", "option_ids": ["x", "y"]},
            "q3": {"kind": "other", "text": "custom"},
            "q4": {
                "kind": "multi_with_other",
                "option_ids": ["left"],
                "other_text": "another",
            },
            "q5": {"kind": "skipped"},
        },
        "method": "click",
    }
    assert http.requests[7][2]["json"] == {"kind": "question", "dismissed": True}


async def test_stateful_session_methods_use_public_v1_shapes() -> None:
    goal = {
        "goalId": "goal-1",
        "objective": "Ship the bridge",
        "completionCriterion": "All checks pass",
        "status": "paused",
        "turnsUsed": 3,
        "tokensUsed": 4200,
        "wallClockMs": 65_000,
        "budget": {
            "tokenBudget": 10_000,
            "turnBudget": 8,
            "wallClockBudgetMs": None,
            "remainingTokens": 5800,
            "remainingTurns": 5,
            "remainingWallClockMs": None,
            "tokenBudgetReached": False,
            "turnBudgetReached": False,
            "wallClockBudgetReached": False,
            "overBudget": False,
        },
        "terminalReason": "Waiting for review",
    }
    http = FakeHttpClient(
        [
            _envelope({}),
            _envelope({"messages": [], "status": {}}),
            _envelope(None),
            _envelope(goal),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
            _envelope(_profile_payload()),
        ]
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    )

    await client.compact_session("session-1")
    await client.undo_session("session-1", count=2)
    assert await client.get_goal("session-1") is None
    assert await client.get_goal("session-1") == GoalInfo(
        id="goal-1",
        objective="Ship the bridge",
        completion_criterion="All checks pass",
        status="paused",
        turns_used=3,
        tokens_used=4200,
        wall_clock_ms=65_000,
        budget=GoalBudget(
            token_budget=10_000,
            turn_budget=8,
            wall_clock_budget_ms=None,
            remaining_tokens=5800,
            remaining_turns=5,
            remaining_wall_clock_ms=None,
            token_budget_reached=False,
            turn_budget_reached=False,
            wall_clock_budget_reached=False,
            over_budget=False,
        ),
        terminal_reason="Waiting for review",
    )
    await client.update_profile(
        "session-1", goal_objective="Ship the bridge"
    )
    await client.update_profile("session-1", goal_control="pause")
    await client.update_profile("session-1", goal_control="resume")
    await client.update_profile("session-1", goal_control="cancel")

    assert [(method, url) for method, url, _kwargs in http.requests] == [
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:compact",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1:undo",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/goal",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/goal",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
        (
            "POST",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/profile",
        ),
    ]
    assert http.requests[0][2]["json"] == {}
    assert http.requests[1][2]["json"] == {"count": 2}
    assert http.requests[4][2]["json"] == {
        "agent_config": {"goal_objective": "Ship the bridge"}
    }
    assert http.requests[5][2]["json"] == {
        "agent_config": {"goal_control": "pause"}
    }
    assert http.requests[6][2]["json"] == {
        "agent_config": {"goal_control": "resume"}
    }
    assert http.requests[7][2]["json"] == {
        "agent_config": {"goal_control": "cancel"}
    }


@pytest.mark.parametrize(
    ("instruction", "body"),
    [
        (None, {}),
        ("", {}),
        (" \t\n ", {}),
        (
            'Preserve "API Decisions".\n保留待办事项。',
            {"instruction": 'Preserve "API Decisions".\n保留待办事项。'},
        ),
    ],
)
async def test_compact_passes_optional_instruction(
    instruction: str | None, body: dict[str, str],
) -> None:
    http = FakeHttpClient([_envelope({})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    await client.compact_session("session-1", instruction=instruction)

    method, url, kwargs = http.requests[0]
    assert method == "POST"
    assert url == "http://127.0.0.1:43123/api/v1/sessions/session-1:compact"
    assert kwargs["json"] == body


async def test_undo_validates_count_and_surfaces_upstream_error() -> None:
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient(
            [_envelope(None, code=40911, msg="Nothing to undo")]
        ),
    )

    for count in (0, -1, True):
        with pytest.raises(ValueError, match="positive integer"):
            await client.undo_session("session-1", count=count)  # type: ignore[arg-type]
    with pytest.raises(KimiServerAPIError, match="Nothing to undo") as exc_info:
        await client.undo_session("session-1")
    assert exc_info.value.code == 40911


async def test_interaction_response_does_not_hide_validation_errors() -> None:
    http = FakeHttpClient(
        [
            _envelope(None, code=40001, msg="invalid query"),
            _envelope(None, code=40001, msg="invalid query"),
        ]
    )
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerAPIError):
        await client.resolve_approval("session-1", "approval-1", "approved")
    with pytest.raises(KimiServerAPIError):
        await client.dismiss_question("session-1", "question-1")


async def test_unknown_server_version_warns_and_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    http = FakeHttpClient([_envelope({"server_version": "0.28.0"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with caplog.at_level("WARNING"):
        assert await client.check_server_version() == "0.28.0"

    assert "UNTESTED KIMI CODE VERSION 0.28.0" in caplog.text


async def test_executable_server_version_mismatch_is_rejected() -> None:
    http = FakeHttpClient([_envelope({"server_version": "0.28.0"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(
        KimiServerStartupError,
        match="executable 0.28.1, server 0.28.0",
    ):
        await client.check_server_version(executable_version="0.28.1")


async def test_supervised_client_uses_preflight_version_for_mismatch() -> None:
    connection = ServerConnection(
        base_url="http://127.0.0.1:43123",
        port=43123,
        generation=1,
        token="secret",
    )
    supervisor = FakeVersionedSupervisor([connection], version="0.28.1")
    client = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=FakeHttpClient([_envelope({"server_version": "0.29.0"})]),
    )

    with pytest.raises(
        KimiServerStartupError,
        match="executable 0.28.1, server 0.29.0",
    ):
        await client.check_server_version()


async def test_malformed_server_version_is_rejected() -> None:
    http = FakeHttpClient([_envelope({"server_version": "version unknown"})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    with pytest.raises(KimiServerStartupError, match="malformed version"):
        await client.check_server_version()


def _turn_started_ops(seq: int) -> dict[str, Any]:
    return _ops_frame(
        [
            {
                "op": "turn.upsert",
                "turn": {
                    "kind": "turn",
                    "turnId": "t0",
                    "ordinal": 0,
                    "state": "running",
                },
            }
        ],
        seq,
    )


def _turn_ended_ops(seq: int, *, state: str = "completed") -> dict[str, Any]:
    return _ops_frame(
        [
            {
                "op": "turn.upsert",
                "turn": {
                    "kind": "turn",
                    "turnId": "t0",
                    "ordinal": 0,
                    "state": state,
                },
            }
        ],
        seq,
    )


def _error_notice_ops(
    seq: int, code: str, message: str, *, retryable: bool
) -> dict[str, Any]:
    return _ops_frame(
        [
            {
                "op": "marker.upsert",
                "item": {
                    "kind": "marker",
                    "markerId": f"m{seq}",
                    "marker": "notice",
                    "payload": {
                        "level": "error",
                        "message": message,
                        "event": {
                            "code": code,
                            "message": message,
                            "retryable": retryable,
                        },
                    },
                },
            }
        ],
        seq,
    )


def _prompt_completed_ops(seq: int) -> dict[str, Any]:
    return _ops_frame(
        [
            {
                "op": "prompt.upsert",
                "prompt": {
                    "promptId": "prompt-1",
                    "status": "completed",
                },
            }
        ],
        seq,
    )


@pytest.mark.parametrize("output_position", ["before_response", "after_response"])
async def test_task_watch_collects_output_around_acknowledgements(
    output_position: str,
) -> None:
    output = {
        "type": "task.output",
        "session_id": "session-1",
        "agent_id": "main",
        "task_id": "bash-1",
        "offset": 0,
        "text": "task output",
    }
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"]},
    )
    socket._task_output_frames_before_response = (
        [output] if output_position == "before_response" else []
    )
    socket._task_output_frames_after_response = [
        {"type": "goal", "session_id": "session-1", "agent_id": "main"},
        *([output] if output_position == "after_response" else []),
        {"type": "goal", "session_id": "session-1", "agent_id": "main"},
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        task = await client.get_task("session-1", "bash-1")
    assert task.output_preview == output["text"]
    assert task.output_bytes == len(output["text"])
    assert [frame["type"] for frame in socket.sent] == ["client_hello", "task.watch"]


@pytest.mark.parametrize("frame_type", ["task.output", "task"])
async def test_task_watch_returns_while_task_frames_keep_arriving(frame_type: str) -> None:
    class StreamingTaskSocket(FakeWebSocket):
        output_offset = 0

        async def recv(self) -> str:
            if not self._incoming.empty():
                return await super().recv()
            await asyncio.sleep(0.01)
            frame = {
                "type": frame_type,
                "session_id": "session-1",
                "agent_id": "main",
            }
            if frame_type == "task.output":
                frame.update(task_id="bash-1", offset=self.output_offset, text=".")
                self.output_offset += 1
            else:
                frame.update(_task_payload("bash-1"))
                frame["event_created_at"] = "2026-11-05T00:00:00Z"
            return json.dumps(frame)

    socket = StreamingTaskSocket(
        subscribe_payload={"accepted": ["session-1"]},
        task_responses=[{"type": "response", "code": 0, "task": _task_payload("bash-1")}],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
        timeout=2.0,
    ) as client:
        task = await client.get_task("session-1", "bash-1")
    assert task.id == "bash-1"
    assert task.status == "running"
    if frame_type == "task.output":
        assert task.output_preview


async def test_task_watch_deduplicates_byte_offsets_and_bounds_utf8_output() -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    base = {
        "type": "task.output",
        "session_id": "session-1",
        "agent_id": "main",
        "task_id": "bash-1",
    }
    socket._task_output_frames_before_response = [
        {**base, "offset": 100, "text": "你🙂"},
        {**base, "offset": 103, "text": "🙂tail"},
        {**base, "offset": 100, "text": "你🙂"},
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        info = await client.get_task("session-1", "bash-1", output_bytes=6)
    assert info.output_preview == "tail"
    assert info.output_bytes == 111
    assert len(info.output_preview.encode("utf-8")) <= 6


@pytest.mark.parametrize(("offset", "text"), [(5, "gap"), (0, "changed")])
async def test_task_watch_rejects_inconsistent_output_offsets(
    offset: int, text: str
) -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    base = {
        "type": "task.output",
        "session_id": "session-1",
        "agent_id": "main",
        "task_id": "bash-1",
    }
    socket._task_output_frames_before_response = [
        {**base, "offset": 0, "text": "test"},
        {**base, "offset": offset, "text": text},
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        with pytest.raises(KimiServerProtocolError):
            await client.get_task("session-1", "bash-1")


async def test_task_stop_does_not_wait_for_output_or_subscribe() -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
        timeout=0.1,
    ) as client:
        assert await client.cancel_task("session-1", "bash-1")
    assert [frame["type"] for frame in socket.sent] == ["client_hello", "task.stop"]


async def test_acknowledged_task_without_output_keeps_its_state() -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
        timeout=0.05,
    ) as client:
        info = await client.get_task("session-1", "bash-1")
    assert info.status == "completed"


async def test_empty_task_output_preserves_the_native_result_summary() -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    socket._task_output_frames_after_response = [
        {
            "type": "task.output",
            "session_id": "session-1",
            "agent_id": "main",
            "task_id": "bash-1",
            "offset": 0,
            "text": "",
        }
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        info = await client.get_task("session-1", "bash-1")
    assert info.output_preview == "done"
    assert info.output_bytes is None


async def test_task_watch_ignores_output_from_other_sessions_agents_and_tasks() -> None:
    socket = FakeWebSocket(subscribe_payload={"accepted": ["session-1"]})
    base = {
        "type": "task.output",
        "session_id": "session-1",
        "agent_id": "main",
        "task_id": "bash-1",
        "offset": 0,
        "text": "right",
    }
    socket._task_output_frames_after_response = [
        {**base, "session_id": "other", "text": "wrong"},
        {**base, "agent_id": "other", "text": "wrong"},
        {**base, "task_id": "other", "text": "wrong"},
        {"type": "goal"},
        base,
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        assert (await client.get_task("session-1", "bash-1")).output_preview == "right"


async def test_new_subscription_seeds_a_fresh_consumer_with_the_running_prefix() -> None:
    running_turn = {
        "kind": "turn",
        "turnId": "turn-1",
        "ordinal": 1,
        "state": "running",
        "steps": [{
            "stepId": "step-1",
            "ordinal": 1,
            "state": "running",
            "frames": [{
                "frameId": "answer-1", "kind": "text", "role": "assistant", "text": "existing ",
            }],
        }],
    }
    sockets = [
        FakeWebSocket(subscribe_payload={"accepted": ["session-1"]}),
        FakeWebSocket(subscribe_payload={"accepted": ["session-1"]}),
    ]
    for socket in sockets:
        socket._reset_snapshot_items = [running_turn]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect(sockets),
    ) as client:
        for _ in sockets:
            events = _session_events(client, "session-1")
            try:
                baseline = await asyncio.wait_for(anext(events), 1)
                assert baseline["type"] == "resync_required"
                assert baseline["snapshot"]["in_flight_turn"]["assistant_text"] == "existing "
            finally:
                await events.aclose()


async def test_subscribe_v2_resumes_the_session_without_a_status_call() -> None:
    actions: list[str] = []
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=[_turn_ended_ops(2)],
    )
    ws_connect = FakeWebSocketConnect([socket], actions=actions)
    http = FakeHttpClient([], actions=actions)
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )
    events = _session_events(client, "session-1")

    ended = await asyncio.wait_for(anext(events), 1)
    assert (ended["seq"], ended["payload"]["type"]) == (2, "turn.ended")
    await events.aclose()

    assert actions == [
        "ws:ws://127.0.0.1:43123/api/v1/ws",
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/transcript",
    ]
    subscribe = next(
        frame for frame in socket.sent if frame["type"] == "subscribe_v2"
    )
    assert subscribe["payload"]["transcript"] == {"main": "delta", "*": "turn"}
    assert "transcript_since" not in subscribe["payload"]


async def test_streamed_error_before_ack_is_yielded_and_stream_continues() -> None:
    error_ops = _error_notice_ops(
        2, "model.not_configured", "No model configured", retryable=False
    )
    completed_ops = _prompt_completed_ops(3)
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=[error_ops, completed_ops],
        events_before_ack=True,
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = _session_events(client, "session-1")

    error_event = await asyncio.wait_for(anext(events), 1)
    assert error_event["payload"] == {
        "type": "error",
        "code": "model.not_configured",
        "message": "No model configured",
        "retryable": False,
    }
    completed = await asyncio.wait_for(anext(events), 1)
    assert completed["payload"] == {"type": "prompt.completed", "promptId": "prompt-1"}
    await events.aclose()


async def test_replayed_transcript_ops_are_not_yielded_twice() -> None:
    error_ops = _error_notice_ops(
        2, "provider.rate_limit", "The provider rate limit was reached", retryable=True
    )
    completed_ops = _prompt_completed_ops(3)
    accepted = {"accepted": ["session-1"], "not_found": []}
    first = FakeWebSocket(
        subscribe_payload=accepted,
        events=[error_ops],
        disconnect_after_events=True,
    )
    second = FakeWebSocket(
        subscribe_payload=accepted,
        events=[error_ops, completed_ops],
    )

    async def no_sleep(_delay: float) -> None:
        pass

    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([first, second]),
        sleep=no_sleep,
    )
    events = _session_events(client, "session-1")

    error_event = await asyncio.wait_for(anext(events), 1)
    assert error_event["payload"]["type"] == "error"
    completed = await asyncio.wait_for(anext(events), 1)
    assert completed["payload"]["type"] == "prompt.completed"
    await events.aclose()

    subscribe = next(
        frame for frame in second.sent if frame["type"] == "subscribe_v2"
    )
    assert subscribe["payload"]["transcript_since"] == {"main": 2}


async def test_reconnect_recovers_from_one_mismatched_ack() -> None:
    accepted = {"accepted": ["session-1"], "not_found": []}
    first_event = _turn_started_ops(2)
    second_event = _turn_ended_ops(3)
    first = FakeWebSocket(
        subscribe_payload=accepted,
        events=[first_event],
        disconnect_after_events=True,
    )
    mismatched = FakeWebSocket(
        subscribe_payload=accepted,
        mismatched_subscribe_ack=True,
    )
    recovered = FakeWebSocket(
        subscribe_payload=accepted,
        events=[first_event, second_event],
    )
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([first, mismatched, recovered]),
        sleep=record_sleep,
    )
    events = _session_events(client, "session-1")

    started = await asyncio.wait_for(anext(events), 1)
    assert started["payload"]["type"] == "turn.started"
    ended = await asyncio.wait_for(anext(events), 1)
    assert ended["payload"]["type"] == "turn.ended"
    await events.aclose()

    for socket in (mismatched, recovered):
        subscribe = next(
            frame for frame in socket.sent if frame["type"] == "subscribe_v2"
        )
        assert subscribe["payload"]["transcript_since"] == {"main": 2}
    assert sleeps == [0.25, 0.5]


async def test_initial_mismatched_ack_remains_fatal() -> None:
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        mismatched_subscribe_ack=True,
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = _session_events(client, "session-1")

    with pytest.raises(KimiServerProtocolError, match="expected ack for request"):
        await asyncio.wait_for(anext(events), 1)
    await events.aclose()


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {
                "type": "warning",
                "code": "agents-md-oversized",
                "message": "Part of AGENTS.md was omitted",
            },
            SessionNotice(
                "warning",
                "Part of AGENTS.md was omitted",
                code="agents-md-oversized",
            ),
        ),
        (
            {
                "type": "error",
                "code": "provider.rate_limit",
                "message": "The provider rate limit was reached",
                "retryable": True,
            },
            SessionNotice(
                "error",
                "The provider rate limit was reached",
                code="provider.rate_limit",
                retryable=True,
            ),
        ),
        (
            {
                "type": "turn.ended",
                "turnId": 4,
                "reason": "failed",
                "error": {
                    "code": "quota.exhausted",
                    "message": "Quota exhausted",
                    "retryable": False,
                    "details": {"private": "not forwarded"},
                },
            },
            SessionNotice(
                "error",
                "Quota exhausted",
                code="quota.exhausted",
                retryable=False,
            ),
        ),
    ],
)
def test_session_notice_from_event_decodes_safe_fields(
    payload: dict[str, Any], expected: SessionNotice
) -> None:
    assert session_notice_from_event({"payload": payload}) == expected


@pytest.mark.parametrize("reason", ["completed", "cancelled"])
def test_session_notice_from_event_ignores_non_failure_turn_end(
    reason: str,
) -> None:
    event = {
        "payload": {"type": "turn.ended", "turnId": 4, "reason": reason}
    }

    assert session_notice_from_event(event) is None


def test_session_notice_from_event_keeps_failure_without_error_details() -> None:
    notice = session_notice_from_event(
        {"payload": {"type": "turn.ended", "turnId": 4, "reason": "failed"}}
    )

    assert notice is not None
    assert notice.kind == "error"
    assert notice.code is None
    assert notice.retryable is None
    assert notice.message == "The turn failed before completing."


def test_session_notice_from_event_ignores_background_task_lifecycle() -> None:
    event = {
        "payload": {
            "type": "background.task.terminated",
            "taskId": "task-1",
            "status": "failed",
        }
    }

    assert session_notice_from_event(event) is None


@pytest.mark.parametrize(
    "payload",
    [
        {"type": "warning", "message": ""},
        {"type": "error", "code": "bad", "message": "Broken"},
        {"type": "turn.ended", "turnId": 4},
        {
            "type": "turn.ended",
            "turnId": 4,
            "reason": "failed",
            "error": {"code": "bad", "message": "Broken"},
        },
        {"type": "turn.ended", "turnId": 4, "reason": "unknown"},
        {"type": "turn.ended", "turnId": 4, "reason": "blocked"},
    ],
)
def test_session_notice_from_event_rejects_contract_drift(
    payload: dict[str, Any],
) -> None:
    with pytest.raises(KimiServerProtocolError):
        session_notice_from_event({"payload": payload})


async def test_unsequenced_websocket_error_before_ack_remains_fatal() -> None:
    socket = FakeWebSocket(
        subscribe_payload={
            "accepted": ["session-1"],
            "not_found": [],
            "resync_required": [],
            "cursors": {},
        },
        events=[
            {
                "type": "error",
                "payload": {
                    "code": 40001,
                    "msg": "invalid subscription",
                    "fatal": True,
                },
            }
        ],
        events_before_ack=True,
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([_envelope({"busy": False})]),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = _session_events(client, "session-1")

    with pytest.raises(
        KimiServerProtocolError,
        match="WebSocket error 40001: invalid subscription",
    ):
        await asyncio.wait_for(anext(events), 1)
    await events.aclose()


async def test_ops_batches_allow_sequence_gaps() -> None:
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=[_turn_started_ops(2), _turn_ended_ops(6)],
    )
    http = FakeHttpClient([])
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = _session_events(client, "session-1")

    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 2
    assert (await asyncio.wait_for(anext(events), 1))["seq"] == 6
    await events.aclose()


async def test_usage_reads_native_totals_without_a_transcript_subscription() -> None:
    stored = {
        "usage": {
            "input_tokens": 1200,
            "output_tokens": 400,
            "cache_read_tokens": 2000,
            "cache_creation_tokens": 300,
            "context_tokens": 50,
            "context_limit": 1000,
        }
    }
    http = FakeHttpClient([_envelope(stored), _envelope(stored)])
    async with KimiServerClient(
        "http://127.0.0.1:43123", "token-1", http_client=http
    ) as client:
        assert await client.get_session_usage("session-1") == SessionUsage(
            1200, 400, 2000, 300, 50, 1000
        )
        client._usage_frames["session-1"] = {"context_tokens": 75}
        assert await client.get_session_usage("session-1") == SessionUsage(
            1200, 400, 2000, 300, 75, 1000
        )
    assert len(http.requests) == 2


async def test_journal_eviction_resyncs_from_reset_snapshot() -> None:
    first = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=[_turn_started_ops(2)],
        disconnect_after_events=True,
    )
    rebuilt_turn = {
        "kind": "turn",
        "turnId": "t0",
        "ordinal": 0,
        "state": "completed",
        "triggerPromptId": "prompt-1",
        "steps": [
            {
                "kind": "step",
                "stepId": "t0.1",
                "turnId": "t0",
                "ordinal": 1,
                "state": "completed",
                "frames": [
                    {"kind": "text", "role": "assistant", "text": "Rebuilt"}
                ],
            }
        ],
    }
    second = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        reset_on_resume=True,
        reset_seq=8,
        events=[
            _ops_frame(
                [
                    {
                        "op": "turn.upsert",
                        "turn": {
                            "kind": "turn",
                            "turnId": "t1",
                            "ordinal": 1,
                            "state": "completed",
                        },
                    }
                ],
                9,
            )
        ],
    )
    second._reset_snapshot_items = [rebuilt_turn]
    actions: list[str] = []
    ws_connect = FakeWebSocketConnect([first, second], actions=actions)
    http = FakeHttpClient([], actions=actions)

    async def no_sleep(_delay: float) -> None:
        pass

    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
        sleep=no_sleep,
    )
    events = _session_events(client, "session-1")

    assert (
        await asyncio.wait_for(anext(events), 1)
    )["payload"]["type"] == "turn.started"
    resync = await asyncio.wait_for(anext(events), 1)
    assert resync["type"] == "resync_required"
    assert resync["payload"]["reason"] == "transcript_reset"
    assert resync["snapshot"]["as_of_seq"] == 8
    assert resync["snapshot"]["in_flight_turn"] is None
    assert resync["snapshot"]["messages"]["items"] == [
        {
            "role": "assistant",
            "content": [{"type": "text", "text": "Rebuilt"}],
            "prompt_id": "prompt-1",
        }
    ]
    resumed = await asyncio.wait_for(anext(events), 1)
    assert resumed["payload"]["type"] == "turn.ended"
    assert resumed["payload"]["turnId"] == 1
    await events.aclose()

    resume = next(frame for frame in second.sent if frame["type"] == "subscribe_v2")
    assert resume["payload"]["transcript_since"] == {"main": 2}


async def test_reconnect_uses_new_supervisor_generation() -> None:
    first = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        disconnect_after_events=True,
    )
    second = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=[_turn_ended_ops(2)],
    )
    actions: list[str] = []
    ws_connect = FakeWebSocketConnect([first, second], actions=actions)
    http = FakeHttpClient([], actions=actions)
    supervisor = FakeSupervisor(
        [
            ServerConnection(
                base_url="http://127.0.0.1:43123",
                port=43123,
                generation=1,
                token="token-1",
            ),
            ServerConnection(
                base_url="http://127.0.0.1:43124",
                port=43124,
                generation=2,
                token="token-2",
            ),
        ]
    )

    async def fake_sleep(delay: float) -> None:
        actions.append(f"sleep:{delay}")

    client = KimiServerClient(
        supervisor=supervisor,  # type: ignore[arg-type]
        http_client=http,
        ws_connect=ws_connect,
        sleep=fake_sleep,
    )
    events = _session_events(client, "session-1")

    assert (
        await asyncio.wait_for(anext(events), 1)
    )["payload"]["type"] == "turn.ended"
    await events.aclose()

    assert actions == [
        "ws:ws://127.0.0.1:43123/api/v1/ws",
        "http:GET:http://127.0.0.1:43123/api/v1/sessions/session-1/transcript",
        "sleep:0.25",
        "ws:ws://127.0.0.1:43124/api/v1/ws",
        "http:GET:http://127.0.0.1:43124/api/v1/sessions/session-1/transcript",
    ]


async def test_subscription_rejects_not_found_sessions() -> None:
    socket = FakeWebSocket(
        subscribe_payload={"accepted": [], "not_found": ["session-1"]}
    )
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([socket]),
    )
    events = _session_events(client, "session-1")

    with pytest.raises(KimiServerProtocolError, match="did not accept"):
        await asyncio.wait_for(anext(events), 1)
    await events.aclose()


async def test_document_fetch_and_probe_subscription_stay_inside_client_boundary() -> None:
    openapi = {"openapi": "3.0.3", "info": {"title": "REST"}}
    asyncapi = {"asyncapi": "3.1.0", "info": {"title": "WebSocket"}}
    http = FakeHttpClient(
        [
            openapi,
            asyncapi,
        ]
    )
    sockets = [
        FakeWebSocket(subscribe_payload={"accepted": ["session-1"], "not_found": []})
        for _ in range(2)
    ]
    ws_connect = FakeWebSocketConnect(sockets)
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token-1",
        http_client=http,
        ws_connect=ws_connect,
    )

    assert await client.get_openapi_document() == openapi
    assert await client.get_asyncapi_document() == asyncapi
    transcript_seq = await client.probe_subscription("session-1")
    assert transcript_seq == 1
    resumed_seq = await client.probe_subscription(
        "session-1", since=transcript_seq
    )
    assert resumed_seq == 1

    assert [request[:2] for request in http.requests] == [
        ("GET", "http://127.0.0.1:43123/openapi.json"),
        ("GET", "http://127.0.0.1:43123/asyncapi.json"),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/transcript",
        ),
        (
            "GET",
            "http://127.0.0.1:43123/api/v1/sessions/session-1/transcript",
        ),
    ]
    assert all(
        request[2]["headers"] == {"Authorization": "Bearer token-1"}
        for request in http.requests
    )
    for socket in sockets:
        subscribe = next(
            frame for frame in socket.sent if frame["type"] == "subscribe_v2"
        )
        assert subscribe["payload"]["transcript"] == {"main": "delta", "*": "turn"}
        assert subscribe["payload"]["session_id"] == "session-1"
    assert "transcript_since" not in sockets[0].sent[1]["payload"]
    assert sockets[1].sent[1]["payload"]["transcript_since"] == {"main": 1}


async def test_supervisor_restarts_with_exponential_backoff() -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    version = FakeCompletedProcess("0.28.1\n")
    help_output = FakeCompletedProcess(KIMI_CODE_HELP)
    first = FakeProcess(startup)
    second = FakeProcess(startup)
    third = FakeProcess(startup)
    web_help = FakeCompletedProcess(KIMI_WEB_HELP)
    factory = FakeProcessFactory(
        [version, help_output, web_help, first, second, third]
    )
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
        assert (await supervisor.wait_until_ready(after_generation=1)).generation == 2
        second.crash(8)
        assert (await supervisor.wait_until_ready(after_generation=2)).generation == 3
    finally:
        await supervisor.stop()

    assert delays == [0.1, 0.2]
    assert third.terminated is True
    assert factory.calls[0][0] == ("kimi", "--version")
    assert factory.calls[1][0] == ("kimi", "--help")
    assert factory.calls[2][0] == ("kimi", "web", "--help")
    assert factory.calls[3][0] == (
        "kimi",
        "web",
        "--no-open",
        "--host",
        "127.0.0.1",
        "--port",
        "43123",
    )
    assert factory.calls[3][1]["start_new_session"] is True
    assert [call[0] for call in factory.calls].count(("kimi", "--version")) == 1
    assert [call[0] for call in factory.calls].count(("kimi", "--help")) == 1


async def test_supervisor_requested_restart_is_immediate_and_single_flight(
    caplog: pytest.LogCaptureFixture,
) -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    first = FakeProcess(startup)
    second = FakeProcess(startup)
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.34.0\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            first,
            FakeCompletedProcess("0.34.0\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            second,
        ]
    )
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
        sleep=fake_sleep,
    )
    try:
        assert (await supervisor.start()).generation == 1
        with caplog.at_level(
            logging.INFO,
            logger="kimi_bridge.kimi_server.supervisor",
        ):
            first_request = asyncio.create_task(supervisor.restart())
            second_request = asyncio.create_task(supervisor.restart())
            first_connection, second_connection = await asyncio.gather(
                first_request, second_request
            )
        assert first_connection.generation == 2
        assert second_connection == first_connection
    finally:
        await supervisor.stop()

    assert first.terminated is True
    assert second.terminated is True
    assert delays == []
    assert "stopped for requested restart" in caplog.text
    assert "exited unexpectedly" not in caplog.text
    assert [call[0] for call in factory.calls].count(("kimi", "--version")) == 2


async def test_supervisor_restart_interrupts_crash_backoff_without_recycling_recovery(
    caplog: pytest.LogCaptureFixture,
) -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    first = FakeProcess(startup)
    second = FakeProcess(startup)
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.34.0\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            first,
            FakeCompletedProcess("0.34.0\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            second,
        ]
    )
    delays: list[float] = []
    backoff_started = asyncio.Event()
    release_backoff = asyncio.Event()

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        backoff_started.set()
        await release_backoff.wait()

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        initial_backoff=10.0,
        process_factory=factory,
        sleep=fake_sleep,
    )
    try:
        assert (await supervisor.start()).generation == 1
        first.crash(7)
        await backoff_started.wait()
        with caplog.at_level(
            logging.INFO,
            logger="kimi_bridge.kimi_server.supervisor",
        ):
            connection = await supervisor.restart()
        assert connection.generation == 2
        assert second.terminated is False
    finally:
        release_backoff.set()
        await supervisor.stop()

    assert delays == [10.0]
    assert "crash backoff interrupted by restart request" in caplog.text
    assert [call[0] for call in factory.calls].count(
        (
            "kimi",
            "web",
            "--no-open",
            "--host",
            "127.0.0.1",
            "--port",
            "43123",
        )
    ) == 2


async def test_supervisor_revalidates_executable_before_requested_restart() -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    first = FakeProcess(startup)
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.34.0\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            first,
            FakeCompletedProcess("kimi, version 0.34.0\n"),
            FakeCompletedProcess(LEGACY_KIMI_CLI_HELP),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )
    try:
        await supervisor.start()
        with pytest.raises(KimiServerStartupError, match="legacy Python kimi-cli"):
            await supervisor.restart()
        assert first.terminated is False
        assert supervisor.connection.generation == 1
    finally:
        await supervisor.stop()


async def test_supervisor_debug_output_redacts_all_startup_token_lines(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "standalone_secret-123"
    child = FakeProcess(
        f"Kimi server: http://127.0.0.1:43123/#token={secret}"
    )
    child.stdout.feed_data(
        f"\x1b[1mToken:\x1b[0m    {secret}\n".encode()
    )
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            child,
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="kimi_bridge.kimi_server.supervisor",
    ):
        try:
            await supervisor.start()
            await asyncio.sleep(0)
        finally:
            await supervisor.stop()

    supervisor_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "kimi_bridge.kimi_server.supervisor"
    ]
    assert secret not in "\n".join(supervisor_messages)
    assert sum("<redacted>" in message for message in supervisor_messages) >= 2


async def test_supervisor_warns_for_unknown_official_version_and_starts(
    caplog: pytest.LogCaptureFixture,
    unlisted_kimi_code_version: str,
) -> None:
    startup = "Kimi server: http://127.0.0.1:43123/#token=secret"
    child = FakeProcess(startup)
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess(f"{unlisted_kimi_code_version}\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            child,
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    try:
        with caplog.at_level("WARNING"):
            await supervisor.start()
    finally:
        await supervisor.stop()

    assert supervisor.executable_identity.version == unlisted_kimi_code_version
    assert f"UNTESTED KIMI CODE VERSION {unlisted_kimi_code_version}" in caplog.text
    assert [call[0] for call in factory.calls[:3]] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
        ("kimi", "web", "--help"),
    ]


async def test_supervisor_rejects_missing_managed_web_flag_before_start() -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess("--no-open --host"),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with pytest.raises(KimiServerStartupError, match="--port"):
        await supervisor.start()


async def test_supervisor_authentication_failure_points_at_neutral_login_guidance() -> (
    None
):
    child = FakeProcess("Error: authentication required, run kimi login")
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess(KIMI_CODE_HELP),
            FakeCompletedProcess(KIMI_WEB_HELP),
            child,
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with pytest.raises(KimiServerAuthenticationError) as exc_info:
        await supervisor.start()

    message = str(exc_info.value)
    assert "authenticate via /login in the kimi TUI" in message
    assert "~/.kimi-code/config.toml" in message
    assert KIMI_CODE_INSTALL_URL in message


async def test_supervisor_rejects_legacy_product_before_start(
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("kimi, version 1.49.0\n"),
            FakeCompletedProcess(LEGACY_KIMI_CLI_HELP),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with caplog.at_level("WARNING"), pytest.raises(
        KimiServerStartupError, match="legacy Python kimi-cli 1.49.0"
    ):
        await supervisor.start()

    assert "INCOMPATIBLE KIMI PRODUCT" in caplog.text
    assert [call[0] for call in factory.calls] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
    ]


async def test_supervisor_rejects_unrecognized_product_fingerprint() -> None:
    factory = FakeProcessFactory(
        [
            FakeCompletedProcess("0.28.1\n"),
            FakeCompletedProcess("Usage: kimi [options]\n"),
        ]
    )
    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=factory,
    )

    with pytest.raises(KimiServerStartupError, match="product fingerprint"):
        await supervisor.start()

    assert [call[0] for call in factory.calls] == [
        ("kimi", "--version"),
        ("kimi", "--help"),
    ]


async def test_supervisor_reports_missing_kimi_before_start() -> None:
    async def missing_process(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError

    supervisor = KimiServerSupervisor(
        preferred_port=43123,
        process_factory=missing_process,
    )

    with pytest.raises(KimiServerStartupError, match="not installed"):
        await supervisor.start()


async def test_archive_session_calls_native_action_with_escaped_id() -> None:
    http = FakeHttpClient([_envelope({"archived": True})])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)

    await client.archive_session("session /1")

    assert len(http.requests) == 1
    method, url, options = http.requests[0]
    assert (method, url) == (
        "POST", "http://127.0.0.1:43123/api/v1/sessions/session%20%2F1:archive"
    )
    assert options["json"] == {}


@pytest.mark.parametrize("data", [{}, {"archived": False}, {"archived": "true"}])
async def test_archive_session_requires_upstream_confirmation(data: dict[str, Any]) -> None:
    client = KimiServerClient(
        "http://127.0.0.1:43123", "token-1",
        http_client=FakeHttpClient([_envelope(data)]),
    )
    with pytest.raises(KimiServerOperationError):
        await client.archive_session("session-1")


async def test_archive_session_surfaces_native_failure() -> None:
    http = FakeHttpClient([_envelope(None, code=40401, msg="Session does not exist")])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)
    with pytest.raises(KimiServerAPIError) as exc:
        await client.archive_session("missing")
    assert exc.value.code == 40401
    assert len(http.requests) == 1


@pytest.mark.parametrize("archived", [True, False, None])
async def test_archive_lost_acknowledgement_checks_stored_metadata(archived: bool | None) -> None:
    requests: list[tuple[str, str]] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        if request.method == "POST":
            raise httpx.ReadError("Archive acknowledgement lost", request=request)
        if archived is None:
            raise httpx.ConnectError("Server unavailable", request=request)
        return httpx.Response(200, json=_envelope({"id": "session-1", "archived": archived}))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        client = KimiServerClient("http://127.0.0.1:43123", "token", http_client=http)
        if archived is True:
            await client.archive_session("session-1")
        elif archived is None:
            with pytest.raises(SessionArchiveUncertain) as error:
                await client.archive_session("session-1")
            assert isinstance(error.value.__cause__, KimiServerTransportError)
        else:
            with pytest.raises(KimiServerTransportError) as error:
                await client.archive_session("session-1")
            assert isinstance(error.value.__cause__, httpx.ReadError)
    assert requests == [
        ("POST", "/api/v1/sessions/session-1:archive"),
        ("GET", "/api/v1/sessions/session-1"),
    ]


@pytest.mark.parametrize("select_default", [False, True])
async def test_concurrent_secondary_model_updates_use_fresh_configuration(select_default: bool) -> None:
    reading = asyncio.Event()
    release = asyncio.Event()
    initial = {"defaultModel": "fast", "models": {"fast": "Quick tasks"}}
    updated = {
        "defaultModel": "smart" if select_default else "fast",
        "models": {"fast": "Quick tasks", "smart": "" if select_default else "Hard tasks"},
    }

    class PausingHttpClient(FakeHttpClient):
        async def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
            response = await super().request(method, url, **kwargs)
            if len(self.requests) == 1:
                reading.set()
                await release.wait()
            return response

    http = PausingHttpClient([
        _envelope({"secondary_model": {}}),
        _envelope({"secondary_model": initial}),
        _envelope({"secondary_model": initial}),
        _envelope({"secondary_model": updated}),
    ])
    client = KimiServerClient("http://127.0.0.1:43123", "token-1", http_client=http)
    first = asyncio.create_task(client.set_secondary_model_entry("fast", "Quick tasks"))
    await reading.wait()
    second = asyncio.create_task(
        client.set_secondary_model("smart") if select_default
        else client.set_secondary_model_entry("smart", "Hard tasks")
    )
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)
    assert [request[0] for request in http.requests] == ["GET", "POST", "GET", "POST"]
    assert results[0].default_model == "fast"
    assert results[1].default_model == updated["defaultModel"]
    assert dict(results[1].models or ()) == updated["models"]
    patch = http.requests[-1][2]["json"]["secondary_model"]
    assert patch == (
        {"default_model": "smart", "models": {"smart": ""}}
        if select_default else {"models": {"smart": "Hard tasks"}}
    )
