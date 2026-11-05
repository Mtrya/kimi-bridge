"""Transcript-operation projection into semantic session events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .types import KimiServerProtocolError, SessionNotice


_TERMINAL_PROMPT_STATES = frozenset({"completed", "failed"})
_TERMINAL_TURN_STATES = frozenset({"completed", "failed", "cancelled"})


@dataclass(slots=True)
class _FrameState:
    kind: str
    role: str | None = None
    text: str = ""


@dataclass(slots=True)
class _StepState:
    ordinal: int
    state: str | None = None
    usage: dict[str, Any] | None = None


@dataclass(slots=True)
class _TurnState:
    turn_id: str
    ordinal: int
    state: str = "queued"
    prompt_id: str | None = None


@dataclass(slots=True)
class TranscriptProjection:
    """Fold one agent's transcript operations into semantic session events.

    Kimi 2.5 streams ``transcript.reset``/``transcript.ops`` frames whose
    batches carry cumulative sequence numbers. The projection keeps the
    authoritative turn/step/frame state, applies each operation batch once,
    and re-emits the bridge's semantic event vocabulary (turn lifecycle,
    deltas, notices, prompt completion, compaction) on top.
    """

    session_id: str
    seq: int | None = None
    started: bool = False
    _turns: dict[str, _TurnState] = field(default_factory=dict)
    _steps: dict[str, _StepState] = field(default_factory=dict)
    _frames: dict[str, _FrameState] = field(default_factory=dict)
    _prompts: dict[str, str] = field(default_factory=dict)

    def usage_totals(self) -> dict[str, int] | None:
        """Sum the per-step usage records seen for the subscribed agent."""

        if not self._steps:
            return None
        totals = {
            "inputOther": 0,
            "output": 0,
            "inputCacheRead": 0,
            "inputCacheCreation": 0,
        }
        seen = False
        for step in self._steps.values():
            if step.usage is None:
                continue
            seen = True
            for name in totals:
                value = step.usage.get(name)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[name] += int(value)
        return totals if seen else None

    def apply_reset(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Rebuild state from a reset snapshot and report a resync."""

        seq = _required_int(payload, "seq")
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict):
            raise KimiServerProtocolError("transcript.reset snapshot must be an object")
        resync: dict[str, Any] | None = None
        if self.started or any(
            isinstance(item, dict)
            and item.get("kind") == "turn"
            and item.get("state") == "running"
            for item in snapshot.get("items") or ()
        ):
            resync = _resync_event(self.session_id, "transcript_reset", snapshot, seq)
        self._turns.clear()
        self._steps.clear()
        self._frames.clear()
        self._prompts.clear()
        items = snapshot.get("items")
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict) or item.get("kind") != "turn":
                    continue
                turn = _turn_from_wire(item)
                self._turns[turn.turn_id] = turn
                for step in item.get("steps") or ():
                    if not isinstance(step, dict):
                        continue
                    step_state = _step_from_wire(step)
                    self._steps[step["stepId"]] = step_state
                    for frame in step.get("frames") or ():
                        if not isinstance(frame, dict):
                            continue
                        frame_id = frame.get("frameId")
                        if isinstance(frame_id, str):
                            self._frames[frame_id] = _frame_from_wire(frame)
        prompts = snapshot.get("prompts")
        if isinstance(prompts, list):
            for prompt in prompts:
                if isinstance(prompt, dict):
                    prompt_id = prompt.get("promptId")
                    status = prompt.get("status")
                    if isinstance(prompt_id, str) and isinstance(status, str):
                        self._prompts[prompt_id] = status
        self.seq = seq
        self.started = True
        return [resync] if resync is not None else []

    def apply_ops(self, payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Apply one operation batch and return its semantic events."""

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
        if kind == "turn.upsert":
            return self._apply_turn_upsert(op, seq)
        if kind == "step.upsert":
            return self._apply_step_upsert(op, seq)
        if kind == "frame.upsert":
            self._apply_frame_upsert(op)
            return []
        if kind == "append":
            return self._apply_append(op, seq)
        if kind == "marker.upsert":
            return self._apply_marker_upsert(op, seq)
        if kind == "prompt.upsert":
            return self._apply_prompt_upsert(op, seq)
        if kind in ("items.remove", "interaction.upsert", "attachment.upsert"):
            return []
        raise KimiServerProtocolError(f"unknown transcript op {kind!r}")

    def _apply_turn_upsert(
        self, op: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        turn = _turn_from_wire(op["turn"])
        previous = self._turns.get(turn.turn_id)
        self._turns[turn.turn_id] = turn
        events: list[dict[str, Any]] = []
        if previous is None or previous.state != turn.state:
            if turn.state == "running":
                events.append(_event(self.session_id, seq, {
                    "type": "turn.started",
                    "turnId": turn.ordinal,
                }))
            elif turn.state in _TERMINAL_TURN_STATES:
                events.append(_event(self.session_id, seq, {
                    "type": "turn.ended",
                    "turnId": turn.ordinal,
                    "reason": turn.state,
                }))
        return events

    def _apply_step_upsert(
        self, op: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        step = _step_from_wire(op["step"])
        step_id = op["step"]["stepId"]
        previous = self._steps.get(step_id)
        self._steps[step_id] = step
        if previous is None and step.state == "running":
            return [
                _event(self.session_id, seq, {
                    "type": "turn.step.started",
                    "step": step.ordinal,
                })
            ]
        return []

    def _apply_frame_upsert(self, op: dict[str, Any]) -> None:
        frame = _frame_from_wire(op["frame"])
        frame_id = op["frame"]["frameId"]
        self._frames[frame_id] = frame

    def _apply_append(self, op: dict[str, Any], seq: int) -> list[dict[str, Any]]:
        target = op.get("target")
        if not isinstance(target, dict):
            raise KimiServerProtocolError("append op target must be an object")
        frame_id = target.get("frameId")
        frame = self._frames.get(frame_id) if isinstance(frame_id, str) else None
        if frame is None:
            return []
        offset = op.get("offset")
        text = op.get("text")
        if not isinstance(offset, int) or isinstance(offset, bool):
            raise KimiServerProtocolError("append op offset must be an integer")
        if not isinstance(text, str):
            raise KimiServerProtocolError("append op text must be a string")
        frame.text = frame.text[:offset] + text
        if frame.kind == "thinking":
            return [
                _event(self.session_id, seq, {
                    "type": "thinking.delta",
                    "delta": text,
                })
            ]
        if frame.kind == "text" and frame.role == "assistant":
            return [
                _event(self.session_id, seq, {
                    "type": "assistant.delta",
                    "delta": text,
                })
            ]
        return []

    def _apply_marker_upsert(
        self, op: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        item = op.get("item")
        if not isinstance(item, dict):
            raise KimiServerProtocolError("marker.upsert item must be an object")
        marker = item.get("marker")
        payload = item.get("payload")
        if not isinstance(payload, dict):
            raise KimiServerProtocolError("marker payload must be an object")
        if marker == "compaction":
            return self._compaction_events(payload, seq)
        if marker == "notice":
            return self._notice_events(payload, seq)
        return []

    def _compaction_events(
        self, payload: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        phase = payload.get("phase")
        if phase == "started":
            return [
                _event(self.session_id, seq, {
                    "type": "compaction.started",
                    "trigger": payload.get("trigger"),
                })
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
                _event(self.session_id, seq, {
                    "type": "compaction.completed",
                    "result": result,
                })
            ]
        return []

    def _notice_events(
        self, payload: dict[str, Any], seq: int
    ) -> list[dict[str, Any]]:
        level = payload.get("level")
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
            _event(self.session_id, seq, {
                "type": "prompt.completed",
                "promptId": prompt_id,
            })
        ]


def _turn_from_wire(value: dict[str, Any]) -> _TurnState:
    turn_id = value.get("turnId")
    ordinal = value.get("ordinal")
    state = value.get("state")
    if not isinstance(turn_id, str) or not turn_id:
        raise KimiServerProtocolError("turn has an invalid turnId")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise KimiServerProtocolError("turn has an invalid ordinal")
    if not isinstance(state, str):
        raise KimiServerProtocolError("turn has an invalid state")
    prompt_id = value.get("triggerPromptId")
    return _TurnState(
        turn_id=turn_id,
        ordinal=ordinal,
        state=state,
        prompt_id=prompt_id if isinstance(prompt_id, str) else None,
    )


def _step_from_wire(value: dict[str, Any]) -> _StepState:
    ordinal = value.get("ordinal")
    state = value.get("state")
    usage = value.get("usage")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool):
        raise KimiServerProtocolError("step has an invalid ordinal")
    return _StepState(
        ordinal=ordinal,
        state=state if isinstance(state, str) else None,
        usage=usage if isinstance(usage, dict) else None,
    )


def _frame_from_wire(value: dict[str, Any]) -> _FrameState:
    kind = value.get("kind")
    role = value.get("role")
    text = value.get("text")
    if not isinstance(kind, str):
        raise KimiServerProtocolError("frame has an invalid kind")
    if not isinstance(text, str):
        text = ""
    return _FrameState(
        kind=kind,
        role=role if isinstance(role, str) else None,
        text=text,
    )


def _event(session_id: str, seq: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "session_event",
        "session_id": session_id,
        "seq": seq,
        "payload": payload,
    }


def _resync_event(
    session_id: str,
    reason: str,
    snapshot: dict[str, Any],
    seq: int,
) -> dict[str, Any]:
    from .transcript import snapshot_from_transcript

    bridge_snapshot = snapshot_from_transcript(
        {
            "items": snapshot.get("items") or [],
            "attachments": snapshot.get("attachments") or [],
            "has_more": bool(snapshot.get("hasMoreOlder")),
            "seq": seq,
        }
    )
    return {
        "type": "resync_required",
        "session_id": session_id,
        "payload": {
            "type": "resync_required",
            "session_id": session_id,
            "reason": reason,
        },
        "snapshot": bridge_snapshot,
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
