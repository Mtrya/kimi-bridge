from __future__ import annotations

import json
from pathlib import Path

import pytest

from kimi_bridge.state import BridgeState, ConversationBinding, StateStore


def test_state_round_trip_uses_versioned_atomic_file(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "state.json"
    store = StateStore(path)
    state = BridgeState(
        bindings={
            "feishu:cli_bot:ou_user": ConversationBinding(
                session_id="session-1",
                workspace="/tmp/project",
                permission_mode="auto",
            )
        }
    )

    store.save(state)

    assert store.load() == state
    assert json.loads(path.read_text(encoding="utf-8"))["version"] == 1
    assert list(path.parent.glob("*.tmp")) == []


def test_missing_state_is_empty(tmp_path: Path) -> None:
    assert StateStore(tmp_path / "missing.json").load() == BridgeState()


@pytest.mark.parametrize("mode", ["manual", "auto", "yolo"])
def test_all_permission_modes_round_trip(tmp_path: Path, mode: str) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {
                    "key": {
                        "session_id": "session-1",
                        "workspace": "/tmp/project",
                        "permission_mode": mode,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    assert StateStore(path).load().bindings["key"].permission_mode == mode


def test_invalid_permission_mode_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {
                    "key": {
                        "session_id": "session-1",
                        "workspace": "/tmp/project",
                        "permission_mode": "unsafe",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="manual, auto, or yolo"):
        StateStore(path).load()
