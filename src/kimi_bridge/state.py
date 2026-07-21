"""Durable conversation-to-session bindings."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path.home() / ".kimi-bridge" / "state.json"
STATE_VERSION = 1
PERMISSION_MODES = frozenset({"manual", "auto", "yolo"})


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    """The durable state associated with one IM conversation."""

    session_id: str
    workspace: str
    permission_mode: str = "manual"


@dataclass(slots=True)
class BridgeState:
    """All durable bridge state."""

    bindings: dict[str, ConversationBinding] = field(default_factory=dict)


class StateStore:
    """Load and atomically replace the bridge state file."""

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()

        with self.path.open(encoding="utf-8") as state_file:
            raw = json.load(state_file)
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise ValueError("unsupported bridge state format")
        bindings_raw = raw.get("bindings")
        if not isinstance(bindings_raw, dict):
            raise TypeError("state bindings must be an object")

        bindings: dict[str, ConversationBinding] = {}
        for conversation_key, value in bindings_raw.items():
            if not isinstance(conversation_key, str) or not isinstance(value, dict):
                raise TypeError("invalid conversation binding")
            session_id = value.get("session_id")
            workspace = value.get("workspace")
            permission_mode = value.get("permission_mode")
            if not isinstance(session_id, str) or not session_id:
                raise TypeError("binding session_id must be a non-empty string")
            if not isinstance(workspace, str) or not workspace:
                raise TypeError("binding workspace must be a non-empty string")
            if permission_mode not in PERMISSION_MODES:
                raise ValueError(
                    "binding permission_mode must be manual, auto, or yolo"
                )
            bindings[conversation_key] = ConversationBinding(
                session_id=session_id,
                workspace=workspace,
                permission_mode=permission_mode,
            )
        return BridgeState(bindings=bindings)

    def save(self, state: BridgeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "bindings": {
                key: {
                    "session_id": binding.session_id,
                    "workspace": binding.workspace,
                    "permission_mode": binding.permission_mode,
                }
                for key, binding in sorted(state.bindings.items())
            },
        }

        descriptor, temporary_name = tempfile.mkstemp(
            dir=self.path.parent,
            prefix=f".{self.path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as state_file:
                json.dump(payload, state_file, ensure_ascii=False, indent=2)
                state_file.write("\n")
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)
