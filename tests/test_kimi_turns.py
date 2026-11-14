from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable
from typing import Any

import httpx
import pytest

from kimi_bridge.interactions import ApprovalRequest
from kimi_bridge.kimi_server import (
    ActivityUpdate,
    InteractionSnapshot,
    KimiServerAPIError,
    KimiServerClient,
    KimiServerOperationError,
    KimiServerProtocolError,
    PlanDocument,
    RecordedTurn,
)
from kimi_bridge.kimi_server.conversation import ConversationProjection
from kimi_bridge.kimi_server.turns import snapshot_from_turns
from kimi_bridge.router.formatting import (
    _in_flight_assistant_text,
    _in_flight_thinking_text,
    _persisted_assistant_text,
    _persisted_thinking_text,
)


NOW = "2026-11-14T00:00:00Z"


def _turn(
    identity: str = "turn-1",
    *,
    ordinal: int = 1,
    kind: str = "agent",
    status: str = "running",
    outcome: str | None = None,
    turn_number: int | None = None,
    steps: Iterable[dict[str, Any]] = (),
    items: Iterable[dict[str, Any]] = (),
) -> dict[str, Any]:
    value = {
        "turn_id": identity,
        "ordinal": ordinal,
        "kind": kind,
        "status": status,
        "started_at": NOW,
        "steps": list(steps),
        "items": list(items),
    }
    if outcome is not None:
        value["outcome"] = outcome
    if turn_number is not None:
        value["turn_number"] = turn_number
    return value


def _step(index: int) -> dict[str, Any]:
    return {"index": index, "outcome": "completed", "started_at": NOW}


def _item(
    identity: str,
    item_type: str = "assistant_message",
    *,
    step: int | None = 1,
    text: str | None = None,
    status: str = "running",
    **extra: Any,
) -> dict[str, Any]:
    value = {
        "type": item_type,
        "item_id": identity,
        "status": status,
        "created_at": NOW,
        **extra,
    }
    if step is not None:
        value["step_index"] = step
    if text is not None:
        value["text"] = text
    return value


def _user(
    identity: str = "user-1",
    *,
    text: str = "hello",
    prompt_ids: Iterable[str] = ("prompt-1",),
    placement: str = "opener",
    origin: dict[str, Any] | None = None,
    attachments: Iterable[dict[str, Any]] = (),
    step: int | None = None,
) -> dict[str, Any]:
    return _item(
        identity,
        "user_message",
        step=step,
        text=text,
        status="ended",
        outcome="completed",
        prompt_ids=list(prompt_ids),
        placement=placement,
        origin=origin or {"kind": "user"},
        attachments=list(attachments),
        skills=[],
    )


def _frame(
    frame_type: str,
    *,
    agent_id: str = "main",
    turn_id: str = "turn-1",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "type": frame_type,
        "session_id": "session-1",
        "agent_id": agent_id,
        "event_created_at": NOW,
        "turn_id": turn_id,
        **extra,
    }


def _page(
    turns: Iterable[dict[str, Any]] = (),
    *,
    agent_id: str = "main",
    has_more: bool = False,
) -> dict[str, Any]:
    return {
        "type": "turns",
        "session_id": "session-1",
        "agent_id": agent_id,
        "event_created_at": NOW,
        "turns": list(turns),
        "has_more": has_more,
    }


def _envelope(data: Any, *, code: int = 0) -> dict[str, Any]:
    return {"code": code, "msg": "test", "data": data, "request_id": "request-1"}


class NativeHttp:
    def __init__(
        self,
        responses: Iterable[dict[str, Any]] | Callable[[httpx.Request], dict[str, Any]] = (),
    ) -> None:
        self._responses = responses
        self._pending = iter(responses) if not callable(responses) else None
        self.requests: list[httpx.Request] = []
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/api/v1/meta":
            data = {"server_version": "2.6.1"}
        elif callable(self._responses):
            data = self._responses(request)
        else:
            assert self._pending is not None
            data = next(self._pending)
        return httpx.Response(200, json=_envelope(data), request=request)

    async def aclose(self) -> None:
        await self.client.aclose()


class TurnsSocket:
    def __init__(
        self,
        *,
        pages: dict[str, list[dict[str, Any]]] | None = None,
        events: Iterable[dict[str, Any]] = (),
        interactions: Iterable[dict[str, Any]] = (),
        response_codes: dict[str, int] | None = None,
        early_frames: dict[str, list[dict[str, Any]]] | None = None,
        disconnect_after_events: bool = False,
    ) -> None:
        self.pages = pages or {}
        self.events = list(events)
        self.interactions = list(interactions)
        self.response_codes = response_codes or {}
        self.early_frames = early_frames or {}
        self.disconnect_after_events = disconnect_after_events
        self.sent: list[dict[str, Any]] = []
        self.received: list[dict[str, Any]] = []
        self.subscribed: set[str] = set()
        self.closed = False
        self._first_subscription = True
        self._incoming: asyncio.Queue[str | BaseException] = asyncio.Queue()

    async def __aenter__(self) -> TurnsSocket:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def send(self, raw: str) -> None:
        frame = json.loads(raw)
        self.sent.append(frame)
        if frame["type"] != "subscribe":
            return
        agent_ids = frame.get("agent_ids") or ["main"]
        key = ",".join(agent_ids)
        for early in self.early_frames.get(key, []):
            self.push(early)
        code = self.response_codes.get(key, 0)
        self.push(
            {
                "type": "response",
                "request_id": frame["request_id"],
                "code": code,
                **({"msg": "subscription rejected"} if code else {}),
            }
        )
        if code:
            return
        for interaction in self.interactions:
            self.push(interaction)
        for agent_id in agent_ids:
            if agent_id in self.subscribed:
                continue
            for burst in _agent_frame(agent_id):
                self.push(burst)
            for page in self.pages.get(agent_id, [_page(agent_id=agent_id)]):
                self.push(page)
        self.subscribed.update(agent_ids)
        if self._first_subscription:
            self._first_subscription = False
            for event in self.events:
                self.push(event)
            if self.disconnect_after_events:
                self.push(OSError("connection lost"))

    def push(self, frame: dict[str, Any] | BaseException) -> None:
        self._incoming.put_nowait(
            frame if isinstance(frame, BaseException) else json.dumps(frame)
        )

    async def recv(self) -> str:
        value = await self._incoming.get()
        if isinstance(value, BaseException):
            raise value
        self.received.append(json.loads(value))
        return value

    async def close(self) -> None:
        self.closed = True


class TurnsConnect:
    def __init__(self, sockets: Iterable[TurnsSocket]) -> None:
        self._sockets = iter(sockets)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, url: str, **kwargs: Any) -> TurnsSocket:
        self.calls.append((url, kwargs))
        return next(self._sockets)


def _agent_frame(agent_id: str) -> list[dict[str, Any]]:
    common = {"session_id": "session-1", "agent_id": agent_id}
    frames = [
        {"type": "task.list", **common, "tasks": []},
        {
            "type": "agent",
            **common,
            "event_created_at": NOW,
            "status": "idle",
            "compacting": False,
            "plan_mode": False,
            "swarm_mode": False,
            "tower_mode": False,
        },
        {
            "type": "usage",
            **common,
            "event_created_at": NOW,
            "context_tokens": 0,
            "max_context_tokens": 1000,
        },
        {"type": "todo", **common, "event_created_at": NOW, "items": []},
    ]
    if agent_id == "main":
        frames.insert(3, {"type": "goal", **common, "event_created_at": NOW})
    return frames


async def _client(
    sockets: Iterable[TurnsSocket],
    responses: Iterable[dict[str, Any]] | Callable[[httpx.Request], dict[str, Any]] = (),
    *,
    timeout: float = 15.0,
) -> tuple[KimiServerClient, NativeHttp, TurnsConnect]:
    http = NativeHttp(responses)
    connect = TurnsConnect(sockets)
    client = KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http.client,
        ws_connect=connect,
        reconnect_initial_backoff=0,
        timeout=timeout,
    )
    assert await client.check_server_version() == "2.6.1"
    return client, http, connect


def _payloads(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [event["payload"] for event in events]


def _tool_with_agents(*agent_ids: str) -> dict[str, Any]:
    return _item(
        "agent-tool",
        "tool_call",
        name="Agent",
        started_at=NOW,
        agent_refs=[{"agent_id": identity, "role": "child"} for identity in agent_ids],
    )


def test_turn_snapshot_preserves_message_boundaries_media_and_prompt_ownership() -> None:
    completed = _turn(
        "opaque-completed",
        ordinal=7,
        status="ended",
        outcome="completed",
        steps=[_step(1), _step(2)],
        items=[
            _user(
                text="Check this",
                prompt_ids=["prompt-7"],
                attachments=[
                    {
                        "kind": "image",
                        "media_type": "image/png",
                        "name": "plot.png",
                        "ordinal": 1,
                    }
                ],
            ),
            _item("thinking-1", "reasoning", text="First thought"),
            _item("answer-1", text="Checking"),
            _item("tool-1", "tool_call", name="ReadFile", started_at=NOW),
            _user(
                "user-steer",
                text="Also check units",
                prompt_ids=["steer-7"],
                placement="steer",
                step=2,
            ),
            _item("thinking-2", "reasoning", step=2, text="Final thought"),
            _item("answer-2a", step=2, text="All "),
            _item("answer-2b", step=2, text="correct"),
        ],
    )
    scheduled = _turn(
        "opaque-scheduled",
        ordinal=8,
        status="ended",
        outcome="completed",
        steps=[_step(1)],
        items=[
            _user(
                "cron-user",
                text="Scheduled check",
                prompt_ids=[],
                origin={"kind": "cron"},
            ),
            _item("cron-answer", text="Scheduled result"),
        ],
    )
    snapshot = snapshot_from_turns({"turns": [completed, scheduled], "has_more": True})

    assert "as_of_seq" not in snapshot
    assert snapshot["turn_ids"] == [7, 8]
    assert snapshot["in_flight_turn"] is None
    assert snapshot["messages"]["has_more"] is True
    assert snapshot["messages"]["items"][0] == {
        "role": "user",
        "prompt_id": "prompt-7",
        "content": [
            {"type": "text", "text": "Check this"},
            {"type": "image/png", "name": "plot.png"},
        ],
    }
    steer = next(
        message
        for message in snapshot["messages"]["items"]
        if message.get("prompt_id") == "steer-7"
    )
    assert steer["role"] == "user"
    assert _persisted_assistant_text(snapshot, prompt_id="prompt-7") == "All correct"
    assert _persisted_thinking_text(snapshot, prompt_id="prompt-7") == "Final thought"
    assert _persisted_assistant_text(snapshot) == "Scheduled result"
    assert "prompt_id" not in snapshot["messages"]["items"][-1]


def test_running_turn_projects_only_current_step_and_tool_only_endings() -> None:
    running = _turn(
        "opaque-running",
        ordinal=9,
        steps=[_step(1)],
        items=[
            _user(prompt_ids=["prompt-9"]),
            _item("old-answer", text="Earlier step"),
            _item("new-thinking", "reasoning", step=2, text="Current thought"),
            _item("new-answer", step=2, text="Current answer"),
        ],
    )
    snapshot = snapshot_from_turns({"turns": [running], "has_more": False})
    assert _in_flight_assistant_text(snapshot, turn_id=9) == "Current answer"
    assert _in_flight_thinking_text(snapshot, turn_id=9) == "Current thought"
    assert snapshot["in_flight_turn"]["current_prompt_id"] == "prompt-9"

    running["status"] = "ended"
    running["outcome"] = "failed"
    running["items"] = [
        _user(),
        _item("old-answer", text="Earlier answer"),
        _item("tool-only", "tool_call", step=2, name="Bash", started_at=NOW),
    ]
    snapshot = snapshot_from_turns({"turns": [running], "has_more": False})
    assert snapshot["in_flight_turn"] is None
    assert _persisted_assistant_text(snapshot) == ""
    assert _persisted_thinking_text(snapshot) is None


def test_projection_streams_steps_corrections_retries_and_prompt_completion() -> None:
    projection = ConversationProjection("session-1")
    assert projection.apply_page(_page()) == []
    assert _payloads(
        projection.apply_frame(_frame("turn.started", turn=_turn()))
    ) == [{"type": "turn.started", "turnId": 1}]
    assert projection.apply_frame(
        _frame("item.ended", item=_user(prompt_ids=["prompt-1", "bundled-1"]))
    ) == []
    assert _payloads(
        projection.apply_frame(
            _frame("item.started", item=_item("thinking", "reasoning", text=""))
        )
    ) == [{"type": "turn.step.started", "step": 1}]
    events = projection.apply_frame(
        _frame("item.delta", item_id="thinking", field="text", delta="Think")
    )
    assert _payloads(events) == [{"type": "thinking.delta", "delta": "Think"}]
    assert events[0]["offset"] == 0
    assert projection.apply_frame(
        _frame("item.started", item=_item("answer", text=""))
    ) == []
    assert _payloads(
        projection.apply_frame(
            _frame("item.delta", item_id="answer", field="text", delta="Hello")
        )
    ) == [{"type": "assistant.delta", "delta": "Hello"}]

    retry = {
        "attempt": 1,
        "next_attempt": 2,
        "max_attempts": 3,
        "delay_ms": 1,
        "error": {"name": "Transport", "message": "temporary"},
    }
    assert projection.apply_frame(_frame("turn.updated", retry=retry)) == []
    assert projection._turns["turn-1"]["retry"] == retry
    assert projection.apply_frame(_frame("turn.updated", step=_step(1))) == []
    assert "retry" not in projection._turns["turn-1"]

    corrected = projection.apply_frame(
        _frame("item.ended", item=_item("answer", text="Corrected", status="ended"))
    )
    assert [event["type"] for event in corrected] == ["resync_required"]
    assert corrected[0]["snapshot"]["in_flight_turn"]["assistant_text"] == "Corrected"
    assert projection.apply_frame(
        _frame("item.ended", item=_item("answer", text="Corrected", status="ended"))
    ) == []
    removed = projection.apply_frame(_frame("item.removed", item_id="answer"))
    assert removed[0]["snapshot"]["in_flight_turn"]["assistant_text"] == ""
    replacement = projection.apply_frame(
        _frame("item.started", item=_item("answer", text="Replacement"))
    )
    assert _payloads(replacement) == [
        {"type": "assistant.delta", "delta": "Replacement"}
    ]

    steering = projection.apply_frame(
        _frame(
            "item.ended",
            item=_user(
                "steer-user",
                text="Change direction",
                prompt_ids=["steer-1"],
                placement="steer",
                step=1,
            ),
        )
    )
    assert steering[0]["snapshot"]["in_flight_turn"]["assistant_text"] == ""
    assert _payloads(
        projection.apply_frame(
            _frame("item.started", item=_item("next-thinking", "reasoning", step=2, text=""))
        )
    ) == [{"type": "turn.step.started", "step": 2}]
    assert _payloads(
        projection.apply_frame(
            _frame("item.delta", item_id="next-thinking", field="text", delta="Next")
        )
    ) == [{"type": "thinking.delta", "delta": "Next"}]
    ended = _turn(
        status="ended",
        outcome="completed",
        steps=[_step(1), _step(2)],
    )
    ended.pop("items")
    assert _payloads(projection.apply_frame(_frame("turn.ended", turn=ended))) == [
        {"type": "turn.ended", "turnId": 1, "reason": "completed"},
        {"type": "prompt.completed", "promptId": "prompt-1"},
        {"type": "prompt.completed", "promptId": "bundled-1"},
        {"type": "prompt.completed", "promptId": "steer-1"},
    ]


def test_projection_replaces_pages_and_removes_turns_by_opaque_identity() -> None:
    projection = ConversationProjection("session-1")
    projection.apply_page(
        _page([_turn("old-id", ordinal=1, items=[_item("old", text="old")])])
    )
    replacement = _turn("new-id", ordinal=1, items=[_item("new", text="new")])
    events = projection.apply_page(_page([replacement]))
    assert [event["type"] for event in events] == ["resync_required"]
    assert events[0]["snapshot"]["turn_ids"] == [1]
    assert events[0]["snapshot"]["in_flight_turn"]["assistant_text"] == "new"

    removed = projection.apply_frame(
        {
            "type": "turns.removed",
            "session_id": "session-1",
            "agent_id": "main",
            "event_created_at": NOW,
            "turn_ids": ["old-id", "new-id"],
        }
    )
    assert removed[0]["snapshot"]["turn_ids"] == []
    assert projection.apply_frame(
        {
            "type": "turns.removed",
            "session_id": "session-1",
            "agent_id": "main",
            "event_created_at": NOW,
            "turn_ids": ["new-id"],
        }
    ) == []


def test_projection_maps_native_compaction_and_notices() -> None:
    projection = ConversationProjection("session-1")
    assert _payloads(projection.apply_agent({"compacting": True}, manual=True)) == [
        {"type": "compaction.started", "trigger": "manual"}
    ]
    assert projection.apply_agent({"compacting": True}, manual=True) == []
    system_turn = _turn(
        "compaction-turn",
        ordinal=2,
        kind="system",
        status="ended",
        outcome="completed",
    )
    system_turn.pop("items")
    assert projection.apply_frame(_frame("turn.started", turn=system_turn)) == []
    compaction = _item(
        "compaction-item",
        "system",
        step=None,
        status="ended",
        outcome="completed",
        kind="compaction",
        trigger="manual",
        compacted_count=3,
        tokens_before=100,
        tokens_after=40,
    )
    running = {**compaction, "status": "running"}
    running.pop("outcome")
    assert projection.apply_frame(
        _frame("item.started", turn_id="compaction-turn", item=running)
    ) == []
    assert _payloads(
        projection.apply_frame(_frame("item.ended", turn_id="compaction-turn", item=compaction))
    ) == [
        {
            "type": "compaction.completed",
            "result": {"compactedCount": 3, "tokensBefore": 100, "tokensAfter": 40},
        }
    ]
    assert projection.apply_frame(
        _frame("item.ended", turn_id="compaction-turn", item=compaction)
    ) == []
    assert _payloads(projection.apply_agent({"compacting": False}, manual=False)) == []
    assert _payloads(projection.apply_agent({"compacting": True}, manual=False)) == [
        {"type": "compaction.started", "trigger": "auto"}
    ]
    cancelled = projection.apply_frame(
        {
            "type": "notice",
            "session_id": "session-1",
            "agent_id": "main",
            "event_created_at": NOW,
            "level": "warning",
            "title": "Compaction cancelled",
            "code": "compaction.cancelled",
        }
    )
    assert _payloads(cancelled) == [
        {"type": "compaction.cancelled"},
        {"type": "warning", "message": "Compaction cancelled", "code": "compaction.cancelled"},
    ]
    failure = projection.apply_frame(
        {
            "type": "notice",
            "session_id": "session-1",
            "agent_id": "main",
            "event_created_at": NOW,
            "level": "error",
            "title": "Provider failed",
            "detail": '{"retryable": true}',
            "code": "provider.unavailable",
        }
    )
    assert _payloads(failure) == [
        {
            "type": "error",
            "message": "Provider failed",
            "code": "provider.unavailable",
            "retryable": True,
        }
    ]


def test_initial_system_compaction_page_does_not_require_live_metrics() -> None:
    projection = ConversationProjection("session-1")
    completed = _turn(
        "system-turn",
        kind="system",
        status="ended",
        outcome="completed",
        items=[
            _item(
                "legacy-compaction",
                "system",
                step=None,
                status="ended",
                outcome="completed",
                kind="compaction",
                trigger="auto",
            )
        ],
    )
    assert projection.apply_page(_page([completed])) == []
    assert projection.snapshot()["messages"]["items"] == []


async def test_native_subscription_has_no_hello_uses_plain_pong_and_replays_interactions() -> None:
    interaction = {
        "type": "interaction",
        "session_id": "session-1",
        "agent_id": "main",
        "event_created_at": NOW,
        "interaction_id": "approval-1",
        "tool_call_id": "tool-1",
        "kind": "approval",
        "status": "pending",
        "request": {"tool_name": "Bash", "action": "Run"},
    }
    socket = TurnsSocket(
        interactions=[interaction],
        events=[{"type": "ping"}, _frame("turn.started", turn=_turn())],
    )
    client, http, _ = await _client([socket])
    stream = client.subscribe_events("session-1")
    try:
        assert await anext(stream) == InteractionSnapshot(
            "session-1", frozenset({"approval-1"})
        )
        update = await anext(stream)
        assert update.id == "approval-1"
        assert (await anext(stream))["payload"] == {
            "type": "turn.started",
            "turnId": 1,
        }
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()

    assert [frame["type"] for frame in socket.sent] == ["subscribe", "pong"]
    assert socket.sent[0]["agent_ids"] == ["main"]
    assert "payload" not in socket.sent[0]
    assert socket.sent[1] == {"type": "pong"}
    assert [frame["type"] for frame in socket.received[:2]] == ["response", "interaction"]
    assert not any("transcript" in request.url.path for request in http.requests)


async def test_native_probe_subscription_uses_authoritative_pages_not_sequences() -> None:
    sockets = [TurnsSocket(), TurnsSocket()]
    client, http, connect = await _client(sockets)
    try:
        assert await client.probe_subscription("session-1") is None
        assert await client.probe_subscription("session-1") is None
    finally:
        await client.aclose()
        await http.aclose()
    assert len(connect.calls) == 2
    assert all(
        frame["type"] == "subscribe" and "transcript_since" not in frame
        for socket in sockets
        for frame in socket.sent
    )
    assert not any("transcript" in request.url.path for request in http.requests)


@pytest.mark.parametrize("failure", ["response", "turns", "ordering"])
async def test_native_subscription_failures_are_operation_errors_without_retry_storms(
    failure: str,
) -> None:
    kwargs: dict[str, Any] = {}
    if failure == "response":
        kwargs["response_codes"] = {"main": 40401}
    elif failure == "turns":
        kwargs["pages"] = {
            "main": [
                {
                    "type": "turns.failed",
                    "session_id": "session-1",
                    "agent_id": "main",
                    "event_created_at": NOW,
                    "code": 40401,
                    "msg": "agent not found",
                }
            ]
        }
    else:
        kwargs["early_frames"] = {"main": [_page()]}
    socket = TurnsSocket(**kwargs)
    client, http, connect = await _client([socket])
    stream = client.subscribe_events("session-1")
    try:
        with pytest.raises(KimiServerOperationError) as caught:
            await asyncio.wait_for(anext(stream), 1)
        assert not isinstance(caught.value, KimiServerProtocolError)
        if failure != "ordering":
            assert isinstance(caught.value, KimiServerAPIError)
            assert caught.value.code == 40401
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()
    assert len(connect.calls) == 1
    assert len(socket.sent) == 1


async def test_subscription_page_timeout_reconnects() -> None:
    first = TurnsSocket(pages={"main": []}, events=[{"type": "ping"}])
    second = TurnsSocket()
    client, http, connect = await _client([first, second], timeout=0.01)
    stream = client.subscribe_events("session-1")
    try:
        assert isinstance(
            await asyncio.wait_for(anext(stream), 1), InteractionSnapshot
        )
        assert len(connect.calls) == 2
        assert [frame["type"] for frame in first.sent] == ["subscribe", "pong"]
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()


async def test_manual_compaction_lifecycle_reaches_semantic_events() -> None:
    socket = TurnsSocket()
    client, http, _ = await _client([socket], [{}])
    stream = client.subscribe_events("session-1")
    try:
        assert isinstance(await anext(stream), InteractionSnapshot)
        await client.compact_session("session-1", instruction="Keep decisions")
        socket.push(
            {
                "type": "agent",
                "session_id": "session-1",
                "agent_id": "main",
                "event_created_at": NOW,
                "status": "idle",
                "compacting": True,
                "plan_mode": False,
            }
        )
        assert (await anext(stream))["payload"] == {
            "type": "compaction.started",
            "trigger": "manual",
        }
        system = _turn(
            "system-compaction",
            kind="system",
            status="ended",
            outcome="completed",
        )
        system.pop("items")
        socket.push(_frame("turn.started", turn_id="system-compaction", turn=system))
        socket.push(
            _frame(
                "item.ended",
                turn_id="system-compaction",
                item=_item(
                    "compaction",
                    "system",
                    step=None,
                    status="ended",
                    outcome="completed",
                    kind="compaction",
                    trigger="manual",
                    compacted_count=2,
                    tokens_before=80,
                    tokens_after=20,
                ),
            )
        )
        assert (await anext(stream))["payload"]["type"] == "compaction.completed"
        assert client._manual_compactions == set()
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()


async def test_native_undo_pages_past_system_and_non_user_turns() -> None:
    newest = _turn(
        "user-new",
        ordinal=3,
        status="ended",
        outcome="completed",
        items=[_user("new-input", prompt_ids=["new-prompt"])],
    )
    system = _turn(
        "system-new",
        ordinal=4,
        kind="system",
        status="ended",
        outcome="completed",
    )
    cron = _turn(
        "cron-new",
        ordinal=2,
        status="ended",
        outcome="completed",
        items=[_user("cron-input", prompt_ids=[], origin={"kind": "cron"})],
    )
    oldest = _turn(
        "user-old",
        ordinal=1,
        status="ended",
        outcome="completed",
        items=[_user("old-input", prompt_ids=["old-prompt"])],
    )
    client, http, _ = await _client(
        [],
        [
            {"turns": [cron, newest, system], "has_more": True},
            {"turns": [oldest], "has_more": False},
            {"removed_turn_ids": ["user-old"]},
        ],
    )
    try:
        await client.undo_session("session-1", count=2)
    finally:
        await client.aclose()
        await http.aclose()
    turns_requests = [request for request in http.requests if request.url.path.endswith("/turns")]
    assert turns_requests[0].url.params["page_size"] == "200"
    assert turns_requests[1].url.params["before_ordinal"] == "2"
    undo = next(request for request in http.requests if request.url.path.endswith(":undo"))
    assert json.loads(undo.content) == {"turn_id": "user-old"}


async def test_native_file_history_uses_turn_number_for_changes_and_content() -> None:
    finished = _turn(
        "recorded-turn",
        ordinal=5,
        status="ended",
        outcome="completed",
        turn_number=41,
        steps=[_step(1)],
        items=[_item("answer", text="done")],
    )

    def respond(request: httpx.Request) -> dict[str, Any]:
        path = request.url.path
        if path.endswith("/turns"):
            return {"turns": [finished], "has_more": False}
        if path.endswith("/file-history/changes"):
            assert request.url.params["turn_number"] == "41"
            assert "turn_id" not in request.url.params
            return {
                "recorded": True,
                "changes": [
                    {
                        "path": "fixture.txt",
                        "status": "modified",
                        "additions": 1,
                        "deletions": 1,
                    }
                ],
            }
        if path.endswith("/file-history/content"):
            assert request.url.params["turn_number"] == "41"
            return {"content": {"content": "old\n" if request.url.params["phase"] == "start" else "new\n"}}
        if path.endswith("/environment"):
            return {"workspace_id": "workspace-1", "environment_id": "local", "cwd": "/workspace"}
        if path.endswith("/workspaces"):
            return {"items": [{"id": "workspace-1", "root": "/workspace", "additional_dirs": []}]}
        if path.endswith("/sessions/session-1"):
            return {"metadata": {"cwd": "/workspace"}}
        raise AssertionError(path)

    client, http, _ = await _client([], respond)
    try:
        view = await client.get_file_changes("session-1")
        assert view.turn == RecordedTurn(41)
        diff = await client.get_file_diff("session-1", "fixture.txt")
        assert diff.text is not None and "-old" in diff.text and "+new" in diff.text
    finally:
        await client.aclose()
        await http.aclose()


async def test_native_plan_review_comes_from_tool_display() -> None:
    plan_turn = _turn(
        "plan-turn",
        status="ended",
        outcome="completed",
        items=[
            _item(
                "plan-tool",
                "tool_call",
                name="ExitPlanMode",
                started_at=NOW,
                display={
                    "kind": "plan_review",
                    "plan": "# Approved plan",
                    "path": "/plans/implementation.md",
                    "options": [],
                    "review": {"state": "pending"},
                },
            )
        ],
    )
    client, http, _ = await _client([], [{"turns": [plan_turn], "has_more": False}])
    try:
        document = await client.get_plan_document(
            ApprovalRequest(
                "approval-1",
                "session-1",
                "ExitPlanMode",
                "Review plan",
                tool_call_id="plan-tool",
            )
        )
        assert document == PlanDocument("implementation.md", "# Approved plan")
    finally:
        await client.aclose()
        await http.aclose()
    assert not any("transcript" in request.url.path for request in http.requests)


async def test_activity_discovers_nested_agents_initially_and_after_reconnect() -> None:
    main = _turn(items=[_user(), _tool_with_agents("child")])
    child = _turn(items=[_tool_with_agents("grandchild")])
    first = TurnsSocket(
        pages={"main": [_page([main])], "child": [_page([child], agent_id="child")]}
    )
    second = TurnsSocket(
        pages={"main": [_page([main])], "child": [_page([child], agent_id="child")]}
    )
    client, http, _ = await _client([first, second])
    stream = client.subscribe_events("session-1", include_activity=True)
    try:
        for socket in (first, second):
            assert isinstance(await anext(stream), InteractionSnapshot)
            baseline = await anext(stream)
            assert isinstance(baseline, ActivityUpdate)
            assert {agent.id for agent in baseline.activity.agents} == {
                "main",
                "child",
                "grandchild",
            }
            assert (await anext(stream))["type"] == "resync_required"
            if socket is first:
                socket.push(OSError("connection lost"))
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()
    for socket in (first, second):
        subscriptions = [frame["agent_ids"] for frame in socket.sent if frame["type"] == "subscribe"]
        assert subscriptions == [["main"], ["child"], ["grandchild"]]
        assert ["*"] not in subscriptions


async def test_activity_discovers_live_agent_refs_and_reconciles_replayed_interactions() -> None:
    main = _turn(items=[_user(), _tool_with_agents()])
    socket = TurnsSocket(pages={"main": [_page([main])]})
    client, http, _ = await _client([socket])
    stream = client.subscribe_events("session-1", include_activity=True)
    try:
        assert isinstance(await anext(stream), InteractionSnapshot)
        assert isinstance(await anext(stream), ActivityUpdate)
        assert (await anext(stream))["type"] == "resync_required"
        socket.push(
            _frame(
                "item.updated",
                item_id="agent-tool",
                set={"agent_refs": [{"agent_id": "child", "role": "child"}]},
            )
        )
        assert await anext(stream) == InteractionSnapshot("session-1", frozenset())
        activity = await anext(stream)
        assert isinstance(activity, ActivityUpdate)
        assert {agent.id for agent in activity.activity.agents} == {"main", "child"}
    finally:
        await stream.aclose()
        await client.aclose()
        await http.aclose()
    assert [frame["agent_ids"] for frame in socket.sent if frame["type"] == "subscribe"] == [
        ["main"],
        ["child"],
    ]


async def test_inspect_activity_discovers_native_child_pages() -> None:
    main = _turn(items=[_tool_with_agents("child")])
    socket = TurnsSocket(pages={"main": [_page([main])]})
    client, http, _ = await _client([socket])
    try:
        activity = await client.inspect_activity("session-1")
        assert {agent.id for agent in activity.agents} == {"main", "child"}
    finally:
        await client.aclose()
        await http.aclose()
    assert [frame["agent_ids"] for frame in socket.sent if frame["type"] == "subscribe"] == [
        ["main"],
        ["child"],
    ]


async def test_side_question_recovers_terminal_turn_from_reconnect_page() -> None:
    running = _turn(
        "side-turn",
        items=[_user(prompt_ids=["side-prompt"]), _item("side-answer", text="Partial")],
    )
    ended = _turn(
        "side-turn",
        status="ended",
        outcome="completed",
        items=[
            _user(prompt_ids=["side-prompt"]),
            _item("side-answer", text="Complete side answer", status="ended"),
        ],
    )
    first = TurnsSocket(
        pages={"side": [_page([running], agent_id="side")]},
        disconnect_after_events=True,
    )
    second = TurnsSocket(pages={"side": [_page([running], agent_id="side")]})
    responses = [
        {"agent_id": "side"},
        {"prompt_id": "side-prompt", "status": "running"},
        {"turns": [running], "has_more": False},
        {"turns": [ended], "has_more": False},
        {"turns": [ended], "has_more": False},
        {"aborted": False},
    ]
    client, http, _ = await _client([first, second], responses)
    try:
        async with asyncio.timeout(1):
            events = [event async for event in client.side_question("session-1", "Explain")]
        terminal = events[-1]
        assert terminal["payload"] == {
            "type": "turn.ended",
            "turnId": 1,
            "reason": "completed",
        }
        assert _persisted_assistant_text(terminal["snapshot"]) == "Complete side answer"
    finally:
        await client.aclose()
        await http.aclose()
    assert not any("transcript" in request.url.path for request in http.requests)


def test_native_projection_rejects_sequence_watermarks_and_unknown_turn_ids() -> None:
    projection = ConversationProjection("session-1")
    assert "seq" not in projection.snapshot()
    with pytest.raises(KimiServerProtocolError):
        projection.apply_frame(_frame("item.removed", turn_id="missing", item_id="item"))
