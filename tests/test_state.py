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
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["bindings"]["feishu:cli_bot:ou_user"]["render_thinking"] is False
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


def test_version_one_state_migrates_without_losing_bindings(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "bindings": {
                    "key": {
                        "session_id": "session-1",
                        "workspace": "/tmp/project",
                        "permission_mode": "manual",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    store = StateStore(path)

    migrated = store.load()

    assert migrated.bindings["key"] == ConversationBinding(
        session_id="session-1",
        workspace="/tmp/project",
        permission_mode="manual",
        render_thinking=False,
    )
    store.save(migrated)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["bindings"]["key"]["render_thinking"] is False


def test_render_thinking_round_trips_in_version_two(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    state = BridgeState(
        bindings={
            "key": ConversationBinding(
                session_id="session-1",
                workspace="/tmp/project",
                render_thinking=True,
            )
        }
    )

    store.save(state)

    assert store.load() == state


def test_unknown_future_state_version_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"version": 3, "bindings": {}}', encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported bridge state format"):
        StateStore(path).load()


def test_version_two_render_thinking_must_be_boolean(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "bindings": {
                    "key": {
                        "session_id": "session-1",
                        "workspace": "/tmp/project",
                        "permission_mode": "manual",
                        "render_thinking": "yes",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeError, match="render_thinking"):
        StateStore(path).load()


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
