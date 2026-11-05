"""Transcript-operation projection into semantic session events."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .transcript import snapshot_from_transcript, stream_text_from_step
from .types import KimiServerProtocolError, SessionNotice


_TERMINAL_PROMPT_STATES = frozenset({"completed", "failed", "aborted"})
_TERMINAL_TURN_STATES = frozenset({"completed", "failed", "cancelled"})
_VISIBLE_MARKERS = frozenset({"compaction", "notice"})


@dataclass(slots=True)
class TranscriptProjection:
    """Reduce one agent's journal into a transcript and semantic output.

    Native turns own their steps and frames. A mutation therefore updates
    the same tree used for both stream deltas and recovery snapshots.
    """

    session_id: str
    seq: int | None = None
    started: bool = False
    _items: dict[str, dict[str, Any]] = field(default_factory=dict)
    _attachments: dict[str, dict[str, Any]] = field(default_factory=dict)
    _prompts: dict[str, str] = field(default_factory=dict)
    _has_more: bool = False

    def snapshot(self, *, seq: int | None = None) -> dict[str, Any]:
        """Return the semantic snapshot of exactly the reduced journal."""

        return snapshot_from_transcript(
            {
                "seq": self.seq if seq is None else seq,
                "items": list(self._items.values()),
                "attachments": list(self._attachments.values()),
                "has_more": self._has_more,
            }
        )

    def apply_reset(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        seq = _required_int(payload, "seq")
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise KimiServerProtocolError("transcript.reset snapshot must be an object")
        was_started = self.started
        items = [
            _copy_item(item)
            for item in snapshot.get("items") or ()
            if item.get("kind") != "marker" or item.get("marker") in _VISIBLE_MARKERS
        ]
        self._items = {_item_id(item): item for item in items}
        self._attachments = {
            item["attachmentId"]: deepcopy(item)
            for item in snapshot.get("attachments") or ()
        }
        self._prompts = {
            prompt["promptId"]: prompt["status"]
            for prompt in snapshot.get("prompts") or ()
        }
        self._has_more = bool(
            payload.get("has_more_older", snapshot.get("hasMoreOlder", False))
        )
        self.seq = seq
        self.started = True
        if was_started or any(item.get("state") == "running" for item in items):
            return [self._resync("transcript_reset", seq)]
        return []

    def apply_ops(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        seq = _required_int(payload, "seq")
        if self.seq is not None and seq <= self.seq:
            return []
        ops = payload.get("ops")
        if not isinstance(ops, list):
            raise KimiServerProtocolError("transcript.ops batch must carry an op list")
        events: list[dict[str, Any]] = []
        for op in ops:
            if not isinstance(op, dict):
                raise KimiServerProtocolError("transcript op must be an object")
            events.extend(self._apply_op(op, seq))
        self.seq = seq
        self.started = True
        return events

    def _apply_op(self, op: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        kind = op.get("op")
        if kind == "reset":
            return self.apply_reset({"seq": seq, "snapshot": op["snapshot"]})
        if kind == "turn.upsert":
            return self._apply_turn_upsert(op, seq)
        if kind == "step.upsert":
            return self._apply_step_upsert(op, seq)
        if kind in ("frame.upsert", "append"):
            return self._apply_frame_change(op, seq)
        if kind == "marker.upsert":
            item = deepcopy(op["item"])
            if item.get("marker") not in _VISIBLE_MARKERS:
                return []
            identity = _item_id(item)
            previous = self._items.get(identity)
            self._items[identity] = item
            if previous == item:
                return []
            payload = item.get("payload") or {}
            if item.get("marker") == "compaction":
                return self._compaction_events(payload, seq)
            if item.get("marker") == "notice":
                return self._notice_events(payload, seq)
            return []
        if kind == "prompt.upsert":
            return self._apply_prompt_upsert(op, seq)
        if kind == "items.remove":
            removed = [
                self._items.pop(identity)
                for identity in op["ids"]
                if identity in self._items
            ]
            return [self._resync("transcript_items_removed", seq)] if removed else []
        if kind == "attachment.upsert":
            attachment = deepcopy(op["attachment"])
            self._attachments[attachment["attachmentId"]] = attachment
            return []
        if kind == "interaction.upsert":
            # Session-wide flat interaction frames own approval/question delivery.
            return []
        raise KimiServerProtocolError(f"unknown transcript op {kind!r}")

    def _apply_turn_upsert(self, op: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        turn = deepcopy(op["turn"])
        identity = _item_id(turn)
        previous = self._items.get(identity)
        turn["steps"] = previous["steps"] if previous is not None else []
        self._items[identity] = turn
        state = turn["state"]
        if previous is not None and previous["state"] == state:
            return []
        if state == "running":
            return [
                _event(
                    self.session_id,
                    seq,
                    {"type": "turn.started", "turnId": turn["ordinal"]},
                )
            ]
        if state in _TERMINAL_TURN_STATES:
            current = self._current_turn()
            if (
                current is not turn
                and current is not None
                and current["state"] == "running"
            ):
                return []
            return [
                _event(
                    self.session_id,
                    seq,
                    {
                        "type": "turn.ended",
                        "turnId": turn["ordinal"],
                        "reason": state,
                    },
                )
            ]
        return []

    def _apply_step_upsert(self, op: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        turn = self._turn(op["turnId"])
        step = deepcopy(op["step"])
        previous = _find(turn["steps"], "stepId", step["stepId"])
        step["frames"] = previous["frames"] if previous is not None else []
        _upsert(turn["steps"], "stepId", step)
        if previous is None:
            return [
                _event(
                    self.session_id,
                    seq,
                    {"type": "turn.step.started", "step": step["ordinal"]},
                )
            ]
        return []

    def _apply_frame_change(self, op: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        target = op if op["op"] == "frame.upsert" else op["target"]
        turn = self._turn(target["turnId"])
        step = _find(turn["steps"], "stepId", target["stepId"])
        if step is None:
            raise KimiServerProtocolError(
                f"transcript references unknown step {target['stepId']!r}"
            )
        before = stream_text_from_step(step)
        if op["op"] == "frame.upsert":
            frame = _copy_frame(op["frame"])
            previous = _find(step["frames"], "frameId", frame["frameId"])
            _upsert(step["frames"], "frameId", frame)
            if frame["kind"] == "notice" and previous != frame:
                return self._notice_events(frame, seq)
        else:
            frame = _find(step["frames"], "frameId", target["frameId"])
            if frame is None:
                raise KimiServerProtocolError(
                    f"transcript references unknown frame {target['frameId']!r}"
                )
            offset = _required_int(op, "offset")
            text = op.get("text")
            if not isinstance(text, str):
                raise KimiServerProtocolError("append text must be a string")
            frame["text"] = _utf16_prefix(frame["text"], offset) + text
        if not self._is_current_step(turn, step):
            return []
        after = stream_text_from_step(step)
        if any(not current.startswith(prior) for prior, current in zip(before, after)):
            return [self._resync("transcript_text_corrected", seq)]
        events = []
        for name, prior, current in zip(
            ("assistant.delta", "thinking.delta"), before, after
        ):
            if current != prior:
                event = _event(
                    self.session_id, seq, {"type": name, "delta": current[len(prior) :]}
                )
                event["offset"] = len(prior)
                events.append(event)
        return events

    def _turn(self, identity: str) -> dict[str, Any]:
        turn = self._items.get(identity)
        if turn is None or turn["kind"] != "turn":
            raise KimiServerProtocolError(
                f"transcript references unknown turn {identity!r}"
            )
        return turn

    def _current_turn(self) -> dict[str, Any] | None:
        """Prefer running work over a later queued turn that has already ended."""

        latest = None
        for item in reversed(self._items.values()):
            if item["kind"] != "turn" or item["state"] == "queued":
                continue
            if item["state"] == "running":
                return item
            if latest is None:
                latest = item
        return latest

    def _is_current_step(self, turn: dict[str, Any], step: dict[str, Any]) -> bool:
        return (
            turn is self._current_turn()
            and bool(turn["steps"])
            and step is turn["steps"][-1]
        )

    def _resync(self, reason: str, seq: int) -> dict[str, Any]:
        return {
            "type": "resync_required",
            "session_id": self.session_id,
            "payload": {
                "type": "resync_required",
                "session_id": self.session_id,
                "reason": reason,
            },
            "snapshot": self.snapshot(seq=seq),
        }

    def _compaction_events(
        self, payload: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        phase = payload.get("phase")
        if phase == "started":
            return [
                _event(
                    self.session_id,
                    seq,
                    {
                        "type": "compaction.started",
                        "trigger": payload.get("trigger"),
                    },
                )
            ]
        if phase in ("blocked", "cancelled"):
            return [_event(self.session_id, seq, {"type": f"compaction.{phase}"})]
        if phase == "completed":
            result = payload.get("result")
            if not isinstance(result, dict):
                raise KimiServerProtocolError(
                    "compaction completed marker has no result"
                )
            return [
                _event(
                    self.session_id,
                    seq,
                    {
                        "type": "compaction.completed",
                        "result": result,
                    },
                )
            ]
        return []

    def _notice_events(self, payload: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        level = payload.get("level")
        if level == "info":
            return []
        message = payload.get("message")
        event = payload.get("event")
        if not isinstance(message, str) or not message:
            raise KimiServerProtocolError("notice marker has no message")
        code = None
        retryable = None
        if isinstance(event, dict):
            code = event.get("code")
            retryable = event.get("retryable")
        if level == "warning":
            notice: dict[str, Any] = {"type": "warning", "message": message}
            if isinstance(code, str) and code:
                notice["code"] = code
            return [_event(self.session_id, seq, notice)]
        notice = {
            "type": "error",
            "message": message,
            "code": code if isinstance(code, str) and code else "error",
            "retryable": retryable if isinstance(retryable, bool) else False,
        }
        return [_event(self.session_id, seq, notice)]

    def _apply_prompt_upsert(
        self, op: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        prompt = op.get("prompt")
        if not isinstance(prompt, dict):
            raise KimiServerProtocolError("prompt.upsert must carry a prompt")
        prompt_id = prompt.get("promptId")
        status = prompt.get("status")
        if not isinstance(prompt_id, str) or not isinstance(status, str):
            raise KimiServerProtocolError("prompt.upsert has invalid identity")
        previous = self._prompts.get(prompt_id)
        self._prompts[prompt_id] = status
        if previous == status or status not in _TERMINAL_PROMPT_STATES:
            return []
        return [
            _event(
                self.session_id,
                seq,
                {
                    "type": "prompt.completed",
                    "promptId": prompt_id,
                },
            )
        ]


def _utf16_prefix(text: str, offset: int) -> str:
    """Translate Kimi's JavaScript string offset before emitting Python offsets."""

    encoded = text.encode("utf-16-le")
    if not 0 <= offset * 2 <= len(encoded):
        raise KimiServerProtocolError("append offset is outside the frame text")
    try:
        return encoded[: offset * 2].decode("utf-16-le")
    except UnicodeDecodeError as exc:
        raise KimiServerProtocolError(
            "append offset splits a UTF-16 character"
        ) from exc


def _copy_frame(frame: dict[str, Any]) -> dict[str, Any]:
    """Retain renderable fields without caching native tool inputs and output."""

    if frame["kind"] not in {"text", "thinking", "tool", "notice"}:
        raise KimiServerProtocolError(
            f"unknown transcript frame kind {frame['kind']!r}"
        )
    return {
        key: deepcopy(value)
        for key, value in frame.items()
        if key
        in {"frameId", "kind", "role", "text", "attachmentIds", "level", "message"}
    }


def _copy_item(item: dict[str, Any]) -> dict[str, Any]:
    copied = deepcopy({key: value for key, value in item.items() if key != "steps"})
    if item["kind"] == "turn":
        copied["steps"] = [
            {
                **deepcopy(
                    {key: value for key, value in step.items() if key != "frames"}
                ),
                "frames": [_copy_frame(frame) for frame in step.get("frames") or ()],
            }
            for step in item.get("steps") or ()
        ]
    return copied


def _item_id(item: dict[str, Any]) -> str:
    key = {"turn": "turnId", "marker": "markerId"}.get(item.get("kind"))
    identity = item.get(key) if key else None
    if not isinstance(identity, str) or not identity:
        raise KimiServerProtocolError("transcript item has invalid kind or identity")
    return identity


def _find(
    items: list[dict[str, Any]], key: str, identity: str
) -> dict[str, Any] | None:
    return next((item for item in items if item[key] == identity), None)


def _upsert(items: list[dict[str, Any]], key: str, value: dict[str, Any]) -> None:
    for index, item in enumerate(items):
        if item[key] == value[key]:
            items[index] = value
            return
    items.append(value)


def _event(session_id: str, seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_event",
        "session_id": session_id,
        "seq": seq,
        "payload": payload,
    }


def _required_int(payload: dict[str, Any], name: str) -> int:
    value = payload.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise KimiServerProtocolError(f"transcript frame has an invalid {name}")
    return value


def session_notice_from_event(event: dict[str, Any]) -> SessionNotice | None:
    """Decode a Kimi event that should be shown to the conversation."""

    payload = event.get("payload")
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type") or event.get("type")
    if event_type == "warning":
        return _warning_notice(payload)
    if event_type == "error":
        return _error_notice(payload)
    if event_type != "turn.ended":
        return None

    reason = payload.get("reason")
    if reason is None:
        raise KimiServerProtocolError("turn.ended reason must be present")
    if reason in ("completed", "cancelled"):
        return None
    if reason == "failed":
        error = payload.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise KimiServerProtocolError("turn.ended error must be an object")
            return _error_notice(error)
        return SessionNotice("error", "The turn failed before completing.")
    raise KimiServerProtocolError(f"turn.ended has unknown reason {reason!r}")


def _warning_notice(payload: dict[str, Any]) -> SessionNotice:
    message = _required_string(payload, "message", event_type="warning")
    code = _optional_string(payload, "code", event_type="warning")
    return SessionNotice("warning", message, code=code)


def _error_notice(payload: dict[str, Any]) -> SessionNotice:
    code = _required_string(payload, "code", event_type="error")
    message = _required_string(payload, "message", event_type="error")
    retryable = payload.get("retryable")
    if not isinstance(retryable, bool):
        raise KimiServerProtocolError("error retryable must be a boolean")
    return SessionNotice("error", message, code=code, retryable=retryable)


def _required_string(
    payload: dict[str, Any], field: str, *, event_type: str
) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise KimiServerProtocolError(
            f"{event_type} {field} must be a non-empty string"
        )
    return value


def _optional_string(
    payload: dict[str, Any], field: str, *, event_type: str
) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise KimiServerProtocolError(
            f"{event_type} {field} must be a non-empty string when present"
        )
    return value
