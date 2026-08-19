"""WebSocket cursor handling and semantic session-event decoding."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .types import KimiServerProtocolError, SessionNotice


@dataclass(slots=True)
class _EventCursor:
    seq: int
    epoch: str | None


def _cursor_from_mapping(value: Any) -> _EventCursor:
    if not isinstance(value, dict):
        raise KimiServerProtocolError("subscription cursor must be an object")
    return _EventCursor(seq=int(value["seq"]), epoch=value.get("epoch"))


def _advance_cursor(
    cursor: _EventCursor | None,
    frame: dict[str, Any],
    *,
    allow_sequence_gaps: bool = False,
) -> Literal["accept", "duplicate", "resync"]:
    seq = frame.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
        raise KimiServerProtocolError("session event has an invalid seq")
    if cursor is None:
        return "accept"

    epoch = frame.get("epoch")
    if epoch is not None and cursor.epoch is not None and epoch != cursor.epoch:
        return "resync"
    if seq <= cursor.seq:
        return "duplicate"
    if not allow_sequence_gaps and seq != cursor.seq + 1:
        return "resync"
    return "accept"


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
    if reason in ("failed", "blocked"):
        error = payload.get("error")
        if error is not None:
            if not isinstance(error, dict):
                raise KimiServerProtocolError("turn.ended error must be an object")
            return _error_notice(error)
        message = (
            "The turn failed before completing."
            if reason == "failed"
            else "The turn was blocked before completing."
        )
        return SessionNotice("error", message)
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
