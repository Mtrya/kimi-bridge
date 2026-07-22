"""WebSocket cursor validation and advancement."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .types import KimiServerProtocolError


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
