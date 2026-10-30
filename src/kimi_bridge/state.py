"""Durable conversation-to-session bindings."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_STATE_PATH = Path.home() / ".kimi-bridge" / "state.json"
STATE_VERSION = 3
MIGRATABLE_STATE_VERSIONS = frozenset({1, 2, STATE_VERSION})
PERMISSION_MODES = frozenset({"manual", "auto", "yolo"})


@dataclass(frozen=True, slots=True)
class DeliveryRoute:
    """The platform address that receives autonomous replies."""

    platform: str
    bot_id: str
    conversation_id: str
    actor_id: str
    actor_aliases: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConversationBinding:
    """The durable state associated with one IM conversation."""

    session_id: str
    workspace: str
    permission_mode: str = "manual"
    render_thinking: bool = False
    delivery: DeliveryRoute | None = None


@dataclass(slots=True)
class BridgeState:
    """All durable bridge state."""

    bindings: dict[str, ConversationBinding] = field(default_factory=dict)
    active_conversation: str | None = None


def _delivery_from_raw(value: object) -> DeliveryRoute | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError("binding delivery must be an object")
    fields: dict[str, str] = {}
    for name in ("platform", "bot_id", "conversation_id", "actor_id"):
        field_value = value.get(name)
        if not isinstance(field_value, str) or not field_value:
            raise TypeError(
                f"binding delivery {name} must be a non-empty string"
            )
        fields[name] = field_value
    aliases = value.get("actor_aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias for alias in aliases
    ):
        raise TypeError("binding delivery actor_aliases must be a list of non-empty strings")
    return DeliveryRoute(**fields, actor_aliases=tuple(aliases))


class StateStore:
    """Load and atomically replace the bridge state file."""

    def __init__(self, path: str | Path = DEFAULT_STATE_PATH) -> None:
        self.path = Path(path).expanduser()

    def load(self) -> BridgeState:
        if not self.path.exists():
            return BridgeState()

        with self.path.open(encoding="utf-8") as state_file:
            raw = json.load(state_file)
        if not isinstance(raw, dict):
            raise ValueError("unsupported bridge state format")
        version = raw.get("version")
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version not in MIGRATABLE_STATE_VERSIONS
        ):
            raise ValueError("unsupported bridge state format")
        active_conversation = (
            raw.get("active_conversation") if version >= 3 else None
        )
        if active_conversation is not None and (
            not isinstance(active_conversation, str) or not active_conversation
        ):
            raise TypeError(
                "active_conversation must be a non-empty string or null"
            )
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
            render_thinking = (
                value.get("render_thinking", False) if version >= 2 else False
            )
            delivery = _delivery_from_raw(
                value.get("delivery") if version >= 3 else None
            )
            if not isinstance(session_id, str) or not session_id:
                raise TypeError("binding session_id must be a non-empty string")
            if not isinstance(workspace, str) or not workspace:
                raise TypeError("binding workspace must be a non-empty string")
            if permission_mode not in PERMISSION_MODES:
                raise ValueError(
                    "binding permission_mode must be manual, auto, or yolo"
                )
            if not isinstance(render_thinking, bool):
                raise TypeError("binding render_thinking must be a boolean")
            bindings[conversation_key] = ConversationBinding(
                session_id=session_id,
                workspace=workspace,
                permission_mode=permission_mode,
                render_thinking=render_thinking,
                delivery=delivery,
            )
        return BridgeState(
            bindings=bindings, active_conversation=active_conversation
        )

    def save(self, state: BridgeState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": STATE_VERSION,
            "active_conversation": state.active_conversation,
            "bindings": {
                key: {
                    "session_id": binding.session_id,
                    "workspace": binding.workspace,
                    "permission_mode": binding.permission_mode,
                    "render_thinking": binding.render_thinking,
                    "delivery": (
                        None
                        if binding.delivery is None
                        else {
                            "platform": binding.delivery.platform,
                            "bot_id": binding.delivery.bot_id,
                            "conversation_id": binding.delivery.conversation_id,
                            "actor_id": binding.delivery.actor_id,
                            "actor_aliases": list(binding.delivery.actor_aliases),
                        }
                    ),
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
