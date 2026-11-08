from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from kimi_bridge.interactions import ApprovalResponse
from kimi_bridge.kimi_server import (
    ActivityUpdate,
    AgentActivity,
    GoalActivity,
    InteractionUpdate,
    InteractionSnapshot,
    KimiServerClient,
    SessionActivity,
)
from kimi_bridge.router import ChatRouter
from kimi_bridge.state import BridgeState, DeliveryRoute, ObservationRoute, StateStore
from test_router import (
    FakeAdapter,
    FakeKimiClient,
    _approval,
    _control_session,
    _event,
    _interaction,
    _message,
    _task,
    _wait_for,
)
from test_session_controls import _router
from test_kimi_server import (
    FakeHttpClient,
    FakeWebSocket,
    FakeWebSocketConnect,
    _envelope,
    _turn_ended_ops,
)


def _activity(
    session_id="session-control",
    *,
    status="running",
    goal=None,
    tasks=(),
    baseline=False,
):
    return ActivityUpdate(
        SessionActivity(
            session_id, (AgentActivity("main", status),), tuple(tasks), goal
        ),
        baseline,
    )


@pytest.mark.parametrize("transition", ["complete", "blocked"])
async def test_goal_and_task_notices_survive_switch_without_changing_binding(
    tmp_path: Path, transition
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other", "title": "Other"},
    ]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    running = _task("fixture-task")
    try:
        await router.handle_inbound(adapter, _message("/status"))
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", "active"), tasks=(running,), baseline=True
            ),
        )
        await _wait_for(lambda: router._active.activity is not None)
        await router.handle_inbound(adapter, _message("/switch other"))
        assert "session-control" in store.load().observations
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", transition),
                tasks=(replace(running, status="completed"),),
            ),
        )
        await _wait_for(lambda: len(adapter.notice_texts) == 2)
        client.emit(
            "session-control",
            _activity(
                goal=GoalActivity("Fixture", transition),
                tasks=(replace(running, status="completed"),),
            ),
        )
        client.emit("other", _event("turn.started", turnId=1))
        client.emit("other", _event("assistant.delta", delta="B answer", offset=0))
        client.emit("other", _event("turn.ended", turnId=1, reason="completed"))
        await _wait_for(lambda: any("B answer" in text for _, _, text in adapter.sent))
        assert len(adapter.notice_texts) == 2
        assert all(
            "session-control" in text and "Control session" in text
            for _, _, text in adapter.notice_texts
        )
        assert store.load().bindings["feishu:cli_bot:ou_user"].session_id == "other"
    finally:
        await router.close()


async def test_notice_failure_is_logged_and_not_retried_or_reconstructed(
    tmp_path: Path, caplog
):
    class FailingNoticeAdapter(FakeAdapter):
        attempts = 0

        async def send_notice_text(self, conversation, text):
            self.attempts += 1
            raise OSError("fixture delivery failed")

    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    router, _ = _router(tmp_path, client)
    adapter = FailingNoticeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        baseline = _activity(goal=GoalActivity("Fixture", "active"), baseline=True)
        terminal = _activity(goal=GoalActivity("Fixture", "complete"))
        client.emit("session-control", baseline)
        client.emit("session-control", terminal)
        await _wait_for(lambda: adapter.attempts == 1)
        client.emit("session-control", terminal)
        client.emit("session-control", replace(terminal, baseline=True))
        client.emit("session-control", _activity(tasks=(), baseline=True))
        await router.handle_inbound(adapter, _message("/status"))
        await asyncio.sleep(0)
        assert adapter.attempts == 1
        assert "dropping" in caplog.text
        assert router._active.task is not None and not router._active.task.done()
    finally:
        await router.close()


async def test_off_session_controls_need_switch_back_and_stop_addresses_binding(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other"},
    ]
    request = replace(_approval(), session_id="session-control")
    client.approvals["session-control"] = [request]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await _wait_for(lambda: len(adapter.interactions) == 1)
        card, _, prompt = adapter.interactions[0]
        await router.handle_inbound(adapter, _message("/switch other"))
        assert len(adapter.notice_texts) == 1
        client.emit(
            "session-control", InteractionUpdate("session-control", request.id, request)
        )
        await asyncio.sleep(0)
        assert len(adapter.notice_texts) == 1 and len(adapter.interactions) == 1
        await router.handle_interaction(
            adapter, _interaction(card, interaction_id=prompt.interaction_id)
        )
        assert client.resolved_approvals == []
        await router.handle_inbound(adapter, _message("/stop"))
        assert client.stopped == ["other"]
        assert client.approvals["session-control"] == [request]
        await router.handle_inbound(adapter, _message("/switch session-control"))
        await _wait_for(lambda: len(adapter.interactions) == 2)
        card2, _, prompt2 = adapter.interactions[-1]
        await router.handle_interaction(
            adapter,
            _interaction(
                card2,
                interaction_id=prompt2.interaction_id,
                response=ApprovalResponse("approved"),
            ),
        )
        assert client.resolved_approvals == [
            ("session-control", request.id, "approved")
        ]
    finally:
        await router.close()


async def test_background_plan_is_automatically_approved_for_noninteractive_adapter(
    tmp_path: Path,
):
    client = FakeKimiClient()
    client.sessions = [
        _control_session(busy=True),
        {**_control_session(), "id": "other"},
    ]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter(supports_interactions=False)
    try:
        await router.handle_inbound(adapter, _message("/status"))
        await router.handle_inbound(adapter, _message("/switch other"))
        request = replace(
            _approval(), session_id="session-control", tool_name="ExitPlanMode"
        )
        client.emit(
            "session-control", InteractionUpdate("session-control", request.id, request)
        )
        await _wait_for(lambda: bool(client.resolved_approvals))
        assert client.resolved_approvals == [
            ("session-control", request.id, "approved")
        ]
        assert adapter.interactions == [] and adapter.notice_texts == []
    finally:
        await router.close()


@pytest.mark.parametrize("version", [1, 2, 3])
def test_observation_state_migrates_existing_bindings(version, tmp_path: Path):
    path = tmp_path / "state.json"
    binding = {
        "session_id": "A",
        "workspace": "/fixture",
        "permission_mode": "auto",
        "render_thinking": True,
    }
    path.write_text(
        json.dumps(
            {
                "version": version,
                "active_conversation": "chat",
                "bindings": {"chat": binding},
            }
        )
    )
    store = StateStore(path)
    before = store.load()
    assert before.observations == {}
    route = ObservationRoute(
        "chat", DeliveryRoute("feishu", "cli_bot", "oc_direct", "ou_user")
    )
    before.observations["B"] = route
    store.save(before)
    assert store.load() == before
    assert store.load().bindings["chat"].session_id == "A"


@pytest.mark.parametrize("allowed", [True, False])
async def test_restart_restores_only_allowed_observation_routes(
    tmp_path: Path, allowed
):
    client = FakeKimiClient()
    client.sessions = [_control_session()]
    store = StateStore(tmp_path / "state.json")
    store.save(
        BridgeState(
            observations={
                "session-control": ObservationRoute(
                    "feishu:cli_bot:ou_user",
                    DeliveryRoute(
                        "feishu",
                        "cli_bot" if allowed else "other-bot",
                        "oc_direct",
                        "ou_user",
                    ),
                )
            }
        )
    )
    router = ChatRouter(
        client, state_store=store, default_workspace=tmp_path, model="kimi-code/k3"
    )
    adapter = FakeAdapter()
    try:
        await router.restore(adapter)
        if allowed:
            await _wait_for(lambda: "session-control" in client.subscriptions)
        assert ("session-control" in client.subscriptions) == allowed
        assert router._active is None
        assert not adapter.notice_texts
    finally:
        await router.close()


class SideClient(FakeKimiClient):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.cancelled = []
        self.answers = asyncio.Queue()

    async def side_question(self, session_id, question):
        self.calls.append((session_id, question))
        try:
            yield await self.answers.get()
        finally:
            self.cancelled.append(question)


async def test_side_answer_is_labelled_independent_and_replaced_on_new_question(
    tmp_path: Path,
):
    client = SideClient()
    client.sessions = [_control_session(busy=True)]
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/btw first"))
        await _wait_for(lambda: len(client.calls) == 1)
        main = router._active
        await router.handle_inbound(adapter, _message("/btw second"))
        await _wait_for(lambda: len(client.calls) == 2)
        assert client.cancelled == ["first"]
        await router.handle_inbound(adapter, _message("main steering"))
        client.answers.put_nowait(
            {
                **_event("turn.ended", reason="completed"),
                "snapshot": {
                    "messages": {
                        "items": [
                            {
                                "role": "assistant",
                                "content": [
                                    {"type": "text", "text": "Side fixture answer"}
                                ],
                            }
                        ]
                    }
                },
            }
        )
        await _wait_for(
            lambda: any("Side fixture answer" in text for _, _, text in adapter.sent)
        )
        assert any("Side question" in text for _, _, text in adapter.sent)
        assert router._active is main and main.render.text == ""
        assert [item[1].text for item in client.prompts] == ["main steering"]
        assert client.calls == [
            ("session-control", "first"),
            ("session-control", "second"),
        ]
    finally:
        await router.close()


async def test_native_side_question_subscribes_and_submits_only_to_created_agent():
    ended = _turn_ended_ops(2)
    ended["payload"]["agent_id"] = "side-agent"
    socket = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []}, events=[ended]
    )
    http = FakeHttpClient(
        [
            _envelope({"agent_id": "side-agent"}),
            _envelope({"prompt_id": "side-prompt", "status": "running"}),
            _envelope(None, code=40903, msg="already ended"),
        ]
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=http,
        ws_connect=FakeWebSocketConnect([socket]),
    ) as client:
        events = [
            event
            async for event in client.side_question("session-1", "Inspect fixture")
        ]
    assert events[-1]["payload"]["type"] == "turn.ended"
    assert "snapshot" in events[-1]
    assert http.requests[0][1].endswith("/sessions/session-1:btw")
    prompt = next(
        request for request in http.requests if request[1].endswith("/prompts")
    )
    assert prompt[2]["json"] == {
        "content": [{"type": "text", "text": "Inspect fixture"}],
        "agent_id": "side-agent",
    }
    subscribe = next(frame for frame in socket.sent if frame["type"] == "subscribe_v2")
    assert subscribe["payload"]["transcript"] == {"side-agent": "delta"}
    transcripts = [
        request for request in http.requests if request[1].endswith("/transcript")
    ]
    assert all(
        request[2]["params"]["agent_id"] == "side-agent" for request in transcripts
    )


async def test_native_subscriptions_for_distinct_sessions_progress_independently():
    sockets = [
        FakeWebSocket(subscribe_payload={"accepted": [session_id], "not_found": []})
        for session_id in ("A", "B")
    ]
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect(sockets),
    ) as client:
        streams = [
            client.subscribe_events(session_id, include_activity=True)
            for session_id in ("A", "B")
        ]
        try:
            first = await asyncio.wait_for(
                asyncio.gather(*(anext(stream) for stream in streams)), 1
            )
            assert [
                item.session_id
                for item in first
                if isinstance(item, InteractionSnapshot)
            ] == ["A", "B"]
            baseline = await asyncio.gather(*(anext(stream) for stream in streams))
            assert all(
                isinstance(item, ActivityUpdate) and item.baseline for item in baseline
            )
            assert len(client._active_ws) == 2
        finally:
            await asyncio.gather(*(stream.aclose() for stream in streams))
        assert client._active_ws == {} and client._activities == {}


async def test_reconnect_replaces_activity_with_a_baseline():
    disconnected = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        disconnect_after_events=True,
        tasks=[
            {
                "task_id": "old-task",
                "kind": "shell",
                "status": "running",
                "detached": True,
            }
        ],
    )
    reconnected = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []}
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([disconnected, reconnected]),
        reconnect_initial_backoff=0,
    ) as client:
        stream = client.subscribe_events("session-1", include_activity=True)
        try:
            await anext(stream)
            first = await anext(stream)
            assert (
                isinstance(first, ActivityUpdate)
                and first.activity.tasks[0].id == "old-task"
            )
            second = None
            async with asyncio.timeout(1):
                while second is None:
                    event = await anext(stream)
                    if isinstance(event, ActivityUpdate):
                        second = event
            assert second.baseline and second.activity.tasks == ()
        finally:
            await stream.aclose()


async def test_fork_keeps_source_observation_and_its_notice_route(tmp_path: Path):
    client = FakeKimiClient()
    client.sessions = [_control_session(busy=True)]
    router, store = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/status"))
        client.emit(
            "session-control",
            _activity(goal=GoalActivity("Source", "active"), baseline=True),
        )
        await _wait_for(lambda: router._active.activity is not None)
        await router.handle_inbound(adapter, _message("/fork"))
        assert (
            store.load().bindings["feishu:cli_bot:ou_user"].session_id
            == "forked-session"
        )
        client.emit(
            "session-control", _activity(goal=GoalActivity("Source", "complete"))
        )
        await _wait_for(lambda: bool(adapter.notice_texts))
        assert adapter.notice_texts[0][1] == _message("").conversation
        assert "Source" in adapter.notice_texts[0][2]
        assert client.stopped == [] and client.aborted == []
    finally:
        await router.close()
