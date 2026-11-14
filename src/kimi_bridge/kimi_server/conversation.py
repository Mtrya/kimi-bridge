"""Reduce Kimi 2.6 conversation frames into semantic session events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
import json
from typing import Any

from .turns import snapshot_from_turns, stream_text_from_turn, turn_prompt_ids
from .types import KimiServerProtocolError


@dataclass(slots=True)
class ConversationProjection:
    session_id: str
    started: bool = False
    _turns: dict[str, dict[str, Any]] = field(default_factory=dict)
    _has_more: bool = False
    _compacting: bool = False
    _compaction_trigger: str = "auto"

    def snapshot(self) -> dict[str, Any]:
        return snapshot_from_turns(
            {
                "turns": [
                    deepcopy(turn)
                    for turn in sorted(
                        self._turns.values(), key=lambda item: item["ordinal"]
                    )
                ],
                "has_more": self._has_more,
            }
        )

    def apply_page(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        turns = frame.get("turns")
        if not isinstance(turns, list):
            raise KimiServerProtocolError("turns frame must carry a turn list")
        was_started = self.started
        replacement: dict[str, dict[str, Any]] = {}
        for value in turns:
            turn = _copy_turn(value, with_items=True)
            replacement[turn["turn_id"]] = turn
        self._turns = replacement
        self._has_more = bool(frame.get("has_more", False))
        self.started = True
        if was_started or any(turn["status"] == "running" for turn in turns):
            return [self._resync("conversation_reset")]
        return []

    def apply_agent(
        self, frame: dict[str, Any], *, manual: bool = False
    ) -> list[dict[str, Any]]:
        compacting = frame.get("compacting")
        if not isinstance(compacting, bool):
            raise KimiServerProtocolError("agent frame has no compacting state")
        started = compacting and not self._compacting
        self._compacting = compacting
        if not started:
            return []
        self._compaction_trigger = "manual" if manual else "auto"
        return [
            _event(
                self.session_id,
                {
                    "type": "compaction.started",
                    "trigger": self._compaction_trigger,
                },
            )
        ]

    def apply_frame(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        frame_type = frame.get("type")
        if frame_type == "turns":
            return self.apply_page(frame)
        if frame_type in {"turn.started", "turn.ended"}:
            return self._apply_turn(frame)
        if frame_type == "turn.updated":
            self._apply_turn_update(frame)
            return []
        if frame_type in {"item.started", "item.ended"}:
            item = frame.get("item")
            if not isinstance(item, dict):
                raise KimiServerProtocolError("conversation item frame has no item")
            return self._apply_item(frame, item)
        if frame_type == "item.updated":
            return self._apply_item_update(frame)
        if frame_type == "item.delta":
            return self._apply_item_delta(frame)
        if frame_type == "item.removed":
            return self._remove_item(frame)
        if frame_type == "turns.removed":
            return self._remove_turns(frame)
        if frame_type == "notice":
            return self._notice_events(frame)
        raise KimiServerProtocolError(f"unknown conversation frame {frame_type!r}")

    def _apply_turn(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        value = frame.get("turn")
        if not isinstance(value, dict):
            raise KimiServerProtocolError("turn frame must carry a turn")
        turn = _copy_turn(value, with_items=False)
        identity = turn["turn_id"]
        previous = self._turns.get(identity)
        previous_header = _without_items(previous) if previous is not None else None
        turn["items"] = previous["items"] if previous is not None else []
        self._turns[identity] = turn
        self.started = True

        if turn["kind"] != "agent":
            return []

        frame_type = frame["type"]
        events: list[dict[str, Any]] = []
        if frame_type == "turn.started" and (
            previous is None or previous["status"] == "ended"
        ):
            events.append(
                _event(
                    self.session_id,
                    {"type": "turn.started", "turnId": turn["ordinal"]},
                )
            )
        if frame_type == "turn.ended" and (
            previous is None
            or previous["status"] != "ended"
            or previous_header != _without_items(turn)
        ):
            if self._is_current_turn(turn):
                events.extend(self._terminal_events(turn))
        return events

    def _apply_turn_update(self, frame: dict[str, Any]) -> None:
        turn = self._turn(_required_string(frame, "turn_id"))
        if "step" in frame:
            step = frame["step"]
            if not isinstance(step, dict):
                raise KimiServerProtocolError("turn.updated step must be an object")
            steps = turn.setdefault("steps", [])
            index = step.get("index")
            for position, existing in enumerate(steps):
                if existing.get("index") == index:
                    steps[position] = deepcopy(step)
                    break
            else:
                steps.append(deepcopy(step))
                steps.sort(key=lambda item: item["index"])
        retry = frame.get("retry")
        if retry is None:
            turn.pop("retry", None)
        elif isinstance(retry, dict):
            turn["retry"] = deepcopy(retry)
        else:
            raise KimiServerProtocolError("turn.updated retry must be an object")

    def _apply_item(
        self, frame: dict[str, Any], value: dict[str, Any]
    ) -> list[dict[str, Any]]:
        turn = self._turn(_required_string(frame, "turn_id"))
        item = deepcopy(value)
        identity = _required_string(item, "item_id")
        items = turn.setdefault("items", [])
        previous_step = stream_text_from_turn(turn)[2]
        previous_item = next(
            (existing for existing in items if existing.get("item_id") == identity),
            None,
        )
        if previous_item == item:
            return []
        before = stream_text_from_turn(turn) if self._is_current_turn(turn) else None
        if previous_item is None:
            items.append(item)
        else:
            items[items.index(previous_item)] = item
        return self._item_change_events(
            turn,
            previous_item,
            item,
            before=before,
            previous_step=previous_step,
        )

    def _apply_item_update(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        turn = self._turn(_required_string(frame, "turn_id"))
        identity = _required_string(frame, "item_id")
        item = self._item(turn, identity)
        changes = frame.get("set")
        if not isinstance(changes, dict):
            raise KimiServerProtocolError("item.updated set must be an object")
        updated = {**item, **deepcopy(changes)}
        return self._apply_item(frame, updated)

    def _apply_item_delta(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        turn = self._turn(_required_string(frame, "turn_id"))
        identity = _required_string(frame, "item_id")
        item = self._item(turn, identity)
        field_name = frame.get("field")
        if field_name not in {"text", "input_text", "output"}:
            raise KimiServerProtocolError("item.delta has an unknown field")
        delta = frame.get("delta")
        if not isinstance(delta, str):
            raise KimiServerProtocolError("item.delta delta must be a string")
        previous = item.get(field_name, "")
        if not isinstance(previous, str):
            raise KimiServerProtocolError("item.delta target is not text")
        return self._apply_item(frame, {**item, field_name: previous + delta})

    def _item_change_events(
        self,
        turn: dict[str, Any],
        previous_item: dict[str, Any] | None,
        item: dict[str, Any],
        *,
        before: tuple[str, str, int | None] | None,
        previous_step: int | None,
    ) -> list[dict[str, Any]]:
        events = self._compaction_events(previous_item, item)
        if before is None:
            return events
        after = stream_text_from_turn(turn)
        new_step = after[2]
        if new_step != previous_step and new_step is not None:
            events.append(
                _event(
                    self.session_id,
                    {"type": "turn.step.started", "step": new_step},
                )
            )
            before = ("", "", new_step)
        if before is None:
            return events
        if after[:2] == before[:2]:
            return events
        if not all(current.startswith(prior) for prior, current in zip(before[:2], after[:2])):
            events.append(self._resync("conversation_text_corrected"))
            return events
        for name, prior, current in zip(
            ("assistant.delta", "thinking.delta"), before[:2], after[:2]
        ):
            if current == prior:
                continue
            event = _event(
                self.session_id,
                {"type": name, "delta": current[len(prior) :]},
            )
            event["offset"] = len(prior)
            events.append(event)
        return events

    def _remove_item(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        turn = self._turn(_required_string(frame, "turn_id"))
        identity = _required_string(frame, "item_id")
        items = turn.setdefault("items", [])
        retained = [item for item in items if item.get("item_id") != identity]
        if len(retained) == len(items):
            return []
        turn["items"] = retained
        return [self._resync("conversation_item_removed")]

    def _remove_turns(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        identities = frame.get("turn_ids")
        if not isinstance(identities, list) or any(
            not isinstance(identity, str) for identity in identities
        ):
            raise KimiServerProtocolError("turns.removed must carry opaque turn IDs")
        removed = False
        for identity in identities:
            removed = self._turns.pop(identity, None) is not None or removed
        return [self._resync("turns_removed")] if removed else []

    def _terminal_events(self, turn: dict[str, Any]) -> list[dict[str, Any]]:
        outcome = turn.get("outcome")
        if outcome not in {"completed", "interrupted", "failed"}:
            raise KimiServerProtocolError("ended turn has no valid outcome")
        reason = {
            "completed": "completed",
            "interrupted": "cancelled",
            "failed": "failed",
        }[outcome]
        payload: dict[str, Any] = {
            "type": "turn.ended",
            "turnId": turn["ordinal"],
            "reason": reason,
        }
        error = turn.get("error")
        if isinstance(error, dict):
            payload["error"] = {
                key: error[key]
                for key in ("code", "message", "retryable")
                if key in error
            }
        events = [_event(self.session_id, payload)]
        events.extend(
            _event(
                self.session_id,
                {"type": "prompt.completed", "promptId": prompt_id},
            )
            for prompt_id in turn_prompt_ids(turn)
        )
        return events

    def _compaction_events(
        self, previous: dict[str, Any] | None, item: dict[str, Any]
    ) -> list[dict[str, Any]]:
        if item.get("type") != "system" or item.get("kind") != "compaction":
            return []
        if previous is not None:
            return []
        if item.get("status") != "ended" or item.get("outcome") != "completed":
            return []
        self._compacting = False
        return [
            _event(
                self.session_id,
                {
                    "type": "compaction.completed",
                    "result": {
                        "compactedCount": _required_number(item, "compacted_count"),
                        "tokensBefore": _required_number(item, "tokens_before"),
                        "tokensAfter": _required_number(item, "tokens_after"),
                    },
                },
            )
        ]

    def _notice_events(self, frame: dict[str, Any]) -> list[dict[str, Any]]:
        level = frame.get("level")
        if level == "info":
            return []
        message = _required_string(frame, "title")
        code = frame.get("code")
        events: list[dict[str, Any]] = []
        if code in {"compaction.cancelled", "compaction.blocked"}:
            self._compacting = False
            events.append(_event(self.session_id, {"type": code}))
        if level == "warning":
            notice: dict[str, Any] = {"type": "warning", "message": message}
            if isinstance(code, str) and code:
                notice["code"] = code
            events.append(_event(self.session_id, notice))
            return events
        if level != "error":
            raise KimiServerProtocolError(f"unknown notice level {level!r}")
        retryable = _notice_retryable(frame.get("detail"))
        events.append(
            _event(
                self.session_id,
                {
                    "type": "error",
                    "message": message,
                    "code": code if isinstance(code, str) and code else "error",
                    "retryable": retryable,
                },
            )
        )
        return events

    def _resync(self, reason: str) -> dict[str, Any]:
        return {
            "type": "resync_required",
            "session_id": self.session_id,
            "payload": {
                "type": "resync_required",
                "session_id": self.session_id,
                "reason": reason,
            },
            "snapshot": self.snapshot(),
        }

    def _is_current_turn(self, turn: dict[str, Any]) -> bool:
        ordered = sorted(self._turns.values(), key=lambda item: item["ordinal"])
        current = next(
            (
                candidate
                for candidate in reversed(ordered)
                if candidate.get("kind") == "agent"
                and candidate.get("status") == "running"
            ),
            None,
        )
        if current is None:
            current = next(
                (
                    candidate
                    for candidate in reversed(ordered)
                    if candidate.get("kind") == "agent"
                ),
                None,
            )
        return current is turn

    def _turn(self, identity: str) -> dict[str, Any]:
        turn = self._turns.get(identity)
        if turn is None:
            raise KimiServerProtocolError(
                f"conversation references unknown turn {identity!r}"
            )
        return turn

    @staticmethod
    def _item(turn: dict[str, Any], identity: str) -> dict[str, Any]:
        item = next(
            (
                value
                for value in turn.get("items") or ()
                if value.get("item_id") == identity
            ),
            None,
        )
        if item is None:
            raise KimiServerProtocolError(
                f"conversation references unknown item {identity!r}"
            )
        return item


def _copy_turn(value: dict[str, Any], *, with_items: bool) -> dict[str, Any]:
    turn = deepcopy(
        value
        if with_items
        else {key: item for key, item in value.items() if key != "items"}
    )
    identity = turn.get("turn_id")
    ordinal = turn.get("ordinal")
    if not isinstance(identity, str) or not identity:
        raise KimiServerProtocolError("conversation turn has no opaque turn_id")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise KimiServerProtocolError("conversation turn has no numeric ordinal")
    if turn.get("kind") not in {"agent", "system"}:
        raise KimiServerProtocolError("conversation turn has an unknown kind")
    if turn.get("status") not in {"running", "ended"}:
        raise KimiServerProtocolError("conversation turn has an unknown status")
    turn.setdefault("items", [])
    turn.setdefault("steps", [])
    return turn


def _without_items(turn: dict[str, Any] | None) -> dict[str, Any] | None:
    if turn is None:
        return None
    return {key: value for key, value in turn.items() if key != "items"}


def _event(session_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "session_event", "session_id": session_id, "payload": payload}


def _required_string(payload: dict[str, Any], name: str) -> str:
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise KimiServerProtocolError(f"conversation frame has an invalid {name}")
    return value


def _required_number(payload: dict[str, Any], name: str) -> int | float:
    value = payload.get(name)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise KimiServerProtocolError(f"compaction item has no {name}")
    return value


def _notice_retryable(detail: Any) -> bool:
    if not isinstance(detail, str):
        return False
    try:
        value = json.loads(detail)
    except json.JSONDecodeError:
        return False
    return isinstance(value, dict) and value.get("retryable") is True
