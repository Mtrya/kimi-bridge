"""Decode semantic session events into user-visible notices."""

from __future__ import annotations

from typing import Any

from .types import KimiServerProtocolError, SessionNotice


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
