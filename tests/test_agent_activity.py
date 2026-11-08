from __future__ import annotations

from pathlib import Path

import pytest

from kimi_bridge.kimi_server import (
    AgentActivity,
    KimiServerClient,
    SessionActivity,
    TodoItem,
)
from kimi_bridge.kimi_server.activity import ActivityProjection
from test_kimi_server import (
    FakeHttpClient,
    FakeWebSocket,
    FakeWebSocketConnect,
    _task_payload,
)
from test_router import FakeAdapter, FakeKimiClient, _message, _control_session
from test_session_controls import _router


def _agent(identifier: str, **fields):
    return {
        "type": "agent",
        "session_id": "session-1",
        "agent_id": identifier,
        "status": "running",
        **fields,
    }


def _tasks(owner: str, *tasks):
    return {
        "type": "task.list",
        "session_id": "session-1",
        "agent_id": owner,
        "tasks": list(tasks),
    }


def _nested_frames():
    return [
        _agent("main", model="kimi-code/k3"),
        _tasks(
            "main",
            {
                **_task_payload("child-task"),
                "kind": "subagent",
                "child_agent_id": "child",
                "description": "Inspect source",
                "detached": False,
            },
        ),
        _agent("child", model="small"),
        _tasks(
            "child",
            {
                **_task_payload("nested-task"),
                "kind": "subagent",
                "child_agent_id": "grandchild",
                "description": "Inspect tests",
                "detached": False,
            },
        ),
        _agent("grandchild"),
        _tasks("grandchild", {**_task_payload("shell-task"), "detached": False}),
        {
            "type": "todo",
            "session_id": "session-1",
            "agent_id": "child",
            "items": [
                {"title": "Read", "status": "done"},
                {"title": "Change", "status": "in_progress"},
                {"title": "Check", "status": "pending"},
            ],
        },
    ]


def test_activity_projects_nested_ownership_and_replacement_snapshots():
    projection = ActivityProjection("session-1")
    for frame in _nested_frames():
        assert projection.apply(frame)
    activity = projection.snapshot()
    agents = {agent.id: agent for agent in activity.agents}
    assert agents["child"].parent_id == "main"
    assert agents["grandchild"].parent_id == "child"
    assert agents["grandchild"].model is None
    assert agents["grandchild"].task.id == "nested-task"
    assert agents["child"].assignment == "Inspect source"
    assert [item.status for item in agents["child"].todos] == [
        "done",
        "in_progress",
        "pending",
    ]
    projection.apply(_tasks("child"))
    projection.apply(
        {"type": "todo", "session_id": "session-1", "agent_id": "child", "items": []}
    )
    assert all(task.id != "nested-task" for task in projection.snapshot().tasks)
    assert (
        next(
            agent for agent in projection.snapshot().agents if agent.id == "child"
        ).todos
        == ()
    )
    assert not projection.apply({**_agent("foreign"), "session_id": "other"})
    assert ActivityProjection("session-1").snapshot().agents == ()


@pytest.mark.parametrize(
    "kind, task_id, owner",
    [
        ("task.watch", "shell-task", "grandchild"),
        ("task.stop", "shell-task", "grandchild"),
        ("task.background", "nested-task", "child"),
    ],
)
async def test_native_task_controls_use_discovered_owner(kind, task_id, owner):
    listing = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        events=_nested_frames(),
        events_before_ack=True,
    )
    task = {**_task_payload(task_id), "detached": True}
    command = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        task_responses=[{"type": "response", "code": 0, "task": task}],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([listing, command]),
    ) as client:
        await client.inspect_activity("session-1")
        # Reuse the projected activity to isolate the subsequent native command.
        if kind == "task.background":
            listing2 = FakeWebSocket(
                subscribe_payload={"accepted": ["session-1"], "not_found": []},
                events=_nested_frames(),
                events_before_ack=True,
            )
            client._ws_connect = FakeWebSocketConnect([listing2, command])
            assert (
                await client.background_agent("session-1", "grandchild")
                == "backgrounded"
            )
        elif kind == "task.watch":
            assert (
                await client.get_task("session-1", task_id, output_bytes=0)
            ).owner_agent_id == owner
        else:
            assert await client.cancel_task("session-1", task_id)
    request = next(frame for frame in command.sent if frame["type"] == kind)
    assert (request["session_id"], request["agent_id"], request["task_id"]) == (
        "session-1",
        owner,
        task_id,
    )
    assert next(frame for frame in listing.sent if frame["type"] == "subscribe_v2")[
        "payload"
    ]["transcript"] == {"*": "turn"}


@pytest.mark.parametrize(
    "before_detached, status, code, expected",
    [
        (False, "running", 0, "backgrounded"),
        (True, "running", 0, "already_background"),
        (False, "ended", 0, "ended"),
        (False, "running", 40933, "unsupported"),
    ],
)
async def test_background_outcomes_follow_native_acknowledgement(
    before_detached, status, code, expected
):
    before = {**_task_payload("task-1"), "detached": before_detached}
    after = {**before, "detached": True, "status": status}
    if status == "ended":
        after["outcome"] = "completed"
    listing = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []}, tasks=[before]
    )
    command = FakeWebSocket(
        subscribe_payload={"accepted": ["session-1"], "not_found": []},
        task_responses=[{"type": "response", "code": code, "task": after}],
    )
    async with KimiServerClient(
        "http://127.0.0.1:43123",
        "token",
        http_client=FakeHttpClient([]),
        ws_connect=FakeWebSocketConnect([listing, command]),
    ) as client:
        assert await client.background_task("session-1", "task-1") == expected


class ActivityClient(FakeKimiClient):
    def __init__(self):
        super().__init__()
        self.sessions = [_control_session()]
        self.activity = SessionActivity(
            "session-control",
            (
                AgentActivity("main", "running", todos=(TodoItem("Main", "pending"),)),
                AgentActivity(
                    "child",
                    "running",
                    "small",
                    "main",
                    "Inspect",
                    todos=(
                        TodoItem("Read", "done"),
                        TodoItem("Change", "in_progress"),
                        TodoItem("Check", "pending"),
                    ),
                ),
            ),
            (),
        )
        self.background_calls = []

    async def inspect_activity(self, session_id):
        return self.activity

    async def background_agent(self, session_id, agent_id):
        self.background_calls.append((session_id, agent_id))
        return "backgrounded"

    async def background_task(self, session_id, task_id):
        self.background_calls.append((session_id, task_id))
        return "already_background"


async def test_agent_commands_scope_choices_and_todos_without_submitting(
    tmp_path: Path,
):
    client = ActivityClient()
    router, _ = _router(tmp_path, client)
    adapter = FakeAdapter()
    try:
        await router.handle_inbound(adapter, _message("/agents"))
        await router.handle_inbound(adapter, _message("/agents show 2"))
        assert "child" in adapter.sent[-1][2] and "Inspect" in adapter.sent[-1][2]
        await router.handle_inbound(adapter, _message("/todos"))
        assert "Main" in adapter.sent[-1][2] and "Read" not in adapter.sent[-1][2]
        await router.handle_inbound(adapter, _message("/todos all"))
        assert all(marker in adapter.sent[-1][2] for marker in ("1/3", "✓", "→", "○"))
        await router.handle_inbound(adapter, _message("/agents background 2"))
        await router.handle_inbound(adapter, _message("/new"))
        await router.handle_inbound(adapter, _message("/agents background 2"))
    finally:
        await router.close()
    assert client.background_calls == [("session-control", "child")]
    assert not client.prompts
