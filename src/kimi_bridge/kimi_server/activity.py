"""Projection of native replacement activity frames, scoped to one connection."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from .types import (
    GoalActivity,
    AgentActivity,
    KimiServerProtocolError,
    SessionActivity,
    TaskInfo,
    TodoItem,
)
from .wire import _task_info_from_wire


class ActivityProjection:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.agents: dict[str, AgentActivity] = {}
        self.tasks: dict[tuple[str, str], TaskInfo] = {}
        self.goal: GoalActivity | None = None

    def apply(self, frame: dict[str, Any]) -> bool:
        if frame.get("session_id") != self.session_id:
            return False
        kind = frame.get("type")
        if kind not in {"agent", "todo", "task", "task.list", "goal"}:
            return False
        owner = frame["agent_id"]
        agent = self.agents.setdefault(owner, AgentActivity(owner, "unknown"))
        if kind == "goal":
            if owner == "main":
                goal = frame.get("goal")
                self.goal = GoalActivity(goal["objective"], goal["status"]) if goal else None
        elif kind == "agent":
            self.agents[owner] = replace(
                agent, status=frame["status"], model=frame.get("model")
            )
        elif kind == "todo":
            items = []
            for item in frame["items"]:
                if item["status"] not in {"pending", "in_progress", "done"}:
                    raise KimiServerProtocolError("unknown todo status")
                items.append(TodoItem(item["title"], item["status"]))
            self.agents[owner] = replace(agent, todos=tuple(items))
        else:
            if kind == "task.list":
                self.tasks = {
                    key: task for key, task in self.tasks.items() if key[0] != owner
                }
                tasks = frame["tasks"]
            else:
                tasks = [frame]
            for value in tasks:
                task = _task_info_from_wire(
                    value, session_id=self.session_id, owner_agent_id=owner
                )
                self.tasks[(owner, task.id)] = task
        return True

    def snapshot(self) -> SessionActivity:
        agents = dict(self.agents)
        for task in self.tasks.values():
            if task.child_agent_id is None:
                continue
            child = agents.get(
                task.child_agent_id,
                AgentActivity(task.child_agent_id, task.status, task.model),
            )
            agents[child.id] = replace(
                child,
                parent_id=task.owner_agent_id,
                assignment=task.description or None,
                task=task,
            )
        return SessionActivity(
            self.session_id, tuple(agents.values()), tuple(self.tasks.values()), self.goal
        )
